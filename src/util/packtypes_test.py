import codecs
import binascii
import struct
import io as StringIO
from io import BytesIO
import hashlib #remove
import os


# ------------------------------------------p2pool memoize------------------------------------------
import itertools


class LRUDict(object):
    def __init__(self, n):
        self.n = n
        self.inner = {}
        self.counter = itertools.count()

    def get(self, key, default=None):
        if key in self.inner:
            x, value = self.inner[key]
            self.inner[key] = self.counter.next(), value
            return value
        return default

    def __setitem__(self, key, value):
        self.inner[key] = self.counter.next(), value
        while len(self.inner) > self.n:
            self.inner.pop(min(self.inner, key=lambda k: self.inner[k][0]))


_nothing = object()


def memoize_with_backing(backing, has_inverses=set()):
    def a(f):
        def b(*args):
            res = backing.get((f, args), _nothing)
            if res is not _nothing:
                return res

            res = f(*args)

            backing[(f, args)] = res
            for inverse in has_inverses:
                backing[(inverse, args[:-1] + (res,))] = args[-1]

            return res
        return b
    return a


class cdict2(dict):
    def __init__(self, func):
        dict.__init__(self)
        self._func = func

    def __missing__(self, key):
        value = self._func(*key)
        self[key] = value
        return value


def fast_memoize_multiple_args(func):
    f = cdict2(func).__getitem__
    return lambda *args: f(args)

# ------------------------------------------p2pool pack-TYPES------------------------------------------
# TODO: remove unnecessary


class EarlyEnd(Exception):
    pass


class LateEnd(Exception):
    pass


def remaining(sio):
    here = sio.tell()
    sio.seek(0, os.SEEK_END)
    end = sio.tell()
    sio.seek(here)
    return end - here


class Type(object):
    __slots__ = []

    def __hash__(self):
        rval = getattr(self, '_hash', None)
        if rval is None:
            try:
                rval = self._hash = hash(
                    (type(self), frozenset(self.__dict__.items())))
            except:
                print(self.__dict__)
                raise
        return rval

    def __eq__(self, other):
        return type(other) is type(self) and other.__dict__ == self.__dict__

    def __ne__(self, other):
        return not (self == other)

    def _unpack(self, data, ignore_trailing=False):
        obj = self.read(data)
        if not ignore_trailing and remaining(data):
            raise LateEnd()
        return obj

    def _pack(self, obj):
        # print(obj)
        # if isinstance(obj,bytes):
        f = BytesIO()
        # else:
        #    f = StringIO.StringIO()
        self.write(f, obj)
        return f.getvalue()

    def unpack(self, data, ignore_trailing=False):
        if isinstance(data, (str, bytes)):
            data = StringIO.BytesIO(data)
        obj = self._unpack(data, ignore_trailing)

        return obj

    def pack(self, obj):
        # No check since obj can have more keys than our type
        return self._pack(obj)

    def packed_size(self, obj):
        if hasattr(obj, '_packed_size') and obj._packed_size is not None:
            type_obj, packed_size = obj._packed_size
            if type_obj is self:
                return packed_size

        packed_size = len(self.pack(obj))

        if hasattr(obj, '_packed_size'):
            obj._packed_size = self, packed_size

        return packed_size


class VarIntType(Type):
    def read(self, file):
        data = file.read(1)
        first = ord(data)
        if first < 0xfd:
            return first
        if first == 0xfd:
            desc, length, minimum = '<H', 2, 0xfd
        elif first == 0xfe:
            desc, length, minimum = '<I', 4, 2**16
        elif first == 0xff:
            desc, length, minimum = '<Q', 8, 2**32
        else:
            raise AssertionError()
        data2 = file.read(length)
        res, = struct.unpack(desc, data2)
        if res < minimum:
            raise AssertionError('VarInt not canonically packed')
        return res

    def write(self, file, item):
        if item < 0xfd:
            return file.write(struct.pack('<B', item))
        elif item <= 0xffff:
            return file.write(struct.pack('<BH', 0xfd, item))
        elif item <= 0xffffffff:
            return file.write(struct.pack('<BI', 0xfe, item))
        elif item <= 0xffffffffffffffff:
            return file.write(struct.pack('<BQ', 0xff, item))
        else:
            raise ValueError('int too large for varint')


class VarStrType(Type):
    _inner_size = VarIntType()

    def read(self, file):
        length = self._inner_size.read(file)
        return file.read(length)

    def write(self, file, item):
        self._inner_size.write(file, len(item))
        file.write(item.encode())


class EnumType(Type):
    def __init__(self, inner, pack_to_unpack):
        self.inner = inner
        self.pack_to_unpack = pack_to_unpack

        self.unpack_to_pack = {}
        for k, v in pack_to_unpack.iteritems():
            if v in self.unpack_to_pack:
                raise ValueError('duplicate value in pack_to_unpack')
            self.unpack_to_pack[v] = k

    def read(self, file):
        data = self.inner.read(file)
        if data not in self.pack_to_unpack:
            raise ValueError('enum data (%r) not in pack_to_unpack (%r)' % (
                data, self.pack_to_unpack))
        return self.pack_to_unpack[data]

    def write(self, file, item):
        if item not in self.unpack_to_pack:
            raise ValueError('enum item (%r) not in unpack_to_pack (%r)' % (
                item, self.unpack_to_pack))
        self.inner.write(file, self.unpack_to_pack[item])


class ListType(Type):
    _inner_size = VarIntType()

    def __init__(self, type, mul=1):
        self.type = type
        self.mul = mul

    def read(self, file):
        length = self._inner_size.read(file)
        length *= self.mul
        res = [self.type.read(file) for i in range(length)]
        return res

    def write(self, file, item):
        assert len(item) % self.mul == 0
        self._inner_size.write(file, len(item)//self.mul)
        for subitem in item:
            self.type.write(file, subitem)


class StructType(Type):
    __slots__ = 'desc length'.split(' ')

    def _pack(self, obj):
        f = BytesIO()  # StringIO.StringIO()
        self.write(f, obj)
        return f.getvalue()

    def __init__(self, desc):
        self.desc = desc
        self.length = struct.calcsize(self.desc)

    def read(self, file):
        data = file.read(self.length)
        return struct.unpack(self.desc, data)[0]

    def write(self, file, item):
        file.write(struct.pack(self.desc, item))


@fast_memoize_multiple_args
class IntType(Type):
    __slots__ = 'bytes step format_str max'.split(' ')

    def _pack(self, obj):
        f = BytesIO()  # StringIO.StringIO()
        print('_pack int type')
        self.write(f, obj)
        return f.getvalue()

    def __new__(cls, bits, endianness='little'):
        assert bits % 8 == 0
        assert endianness in ['little', 'big']
        if bits in [8, 16, 32, 64]:
            return StructType(('<' if endianness == 'little' else '>') + {8: 'B', 16: 'H', 32: 'I', 64: 'Q'}[bits])
        else:
            return Type.__new__(cls)

    def __init__(self, bits, endianness='little'):
        assert bits % 8 == 0
        assert endianness in ['little', 'big']
        self.bytes = bits//8
        self.step = -1 if endianness == 'little' else 1
        self.format_str = '%%0%ix' % (2*self.bytes)
        self.max = 2**bits

    def read(self, file, b2a_hex=binascii.b2a_hex):
        if self.bytes == 0:
            return 0
        data = file.read(self.bytes)
        return int(b2a_hex(data[::self.step]), 16)

    def write(self, file, item, a2b_hex=binascii.a2b_hex):
        if self.bytes == 0:
            return None
        if not 0 <= item < self.max:
            raise ValueError('invalid int value - %r' % (item,))
        file.write(a2b_hex(self.format_str % (item,))[::self.step])


class IPV6AddressType(Type):
    def read(self, file):
        data = file.read(16)
        if data[:12] == codecs.decode('00000000000000000000ffff', 'hex'):
            return '.'.join(str(x) for x in data[12:])
        return ':'.join(data[i*2:(i+1)*2].encode('hex') for i in range(8))

    def write(self, file, item):
        if ':' in item:
            data = codecs.decode(''.join(item.replace(':', '')), 'hex')
        else:
            bits = list(map(int, item.split('.')))
            if len(bits) != 4:
                raise ValueError('invalid address: %r' % (bits,))

            dataA = bytes()
            for x in [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 255, 255]:
                dataA += chr(x).encode('latin-1')
            dataB = bytes()
            for x in bits:
                dataB += struct.pack('B', x)
            data = dataA + dataB
            print(type(data))
        assert len(data) == 16, len(data)
        file.write(data)


_record_types = {}


def get_record(fields):
    fields = tuple(sorted(fields))
    if 'keys' in fields or '_packed_size' in fields:
        raise ValueError()
    if fields not in _record_types:
        class _Record(object):
            __slots__ = fields + ('_packed_size',)

            def __init__(self):
                self._packed_size = None

            def __repr__(self):
                return repr(dict(self))

            def __getitem__(self, key):
                return getattr(self, key)

            def __setitem__(self, key, value):
                setattr(self, key, value)
            # def __iter__(self):
            #    for field in fields:
            #        yield field, getattr(self, field)

            def keys(self):
                return fields

            def get(self, key, default=None):
                return getattr(self, key, default)

            def __eq__(self, other):
                if isinstance(other, dict):
                    return dict(self) == other
                elif isinstance(other, _Record):
                    for k in fields:
                        if getattr(self, k) != getattr(other, k):
                            return False
                    return True
                elif other is None:
                    return False
                raise TypeError()

            def __ne__(self, other):
                return not (self == other)
        _record_types[fields] = _Record
    return _record_types[fields]


class ComposedType(Type):
    def __init__(self, fields):
        self.fields = list(fields)
        self.field_names = set(k for k, v in fields)
        self.record_type = get_record(k for k, v in self.fields)

    def read(self, file):
        item = self.record_type()
        for key, type_ in self.fields:
            item[key] = type_.read(file)
        return item

    def write(self, file, item):
        assert set(item.keys()) >= self.field_names
        for key, type_ in self.fields:
            type_.write(file, item[key])


class PossiblyNoneType(Type):
    def __init__(self, none_value, inner):
        self.none_value = none_value
        self.inner = inner

    def read(self, file):
        value = self.inner.read(file)
        return None if value == self.none_value else value

    def write(self, file, item):
        if item == self.none_value:
            raise ValueError('none_value used')
        self.inner.write(file, self.none_value if item is None else item)


class FixedStrType(Type):
    def __init__(self, length):
        self.length = length

    def read(self, file):
        return file.read(self.length)

    def write(self, file, item):
        if len(item) != self.length:
            raise ValueError('incorrect length item!')
        file.write(item)


# ------------------------------------------messages and types---------------------------------------
address_type = ComposedType([
    ('services', IntType(64)),
    ('address', IPV6AddressType()),
    ('port', IntType(16, 'big')),
])

share_type = ComposedType([
    ('type', VarIntType()),
    ('contents', VarStrType()),
])

block_header_type = ComposedType([
    ('version', IntType(32)),
    ('previous_block', PossiblyNoneType(0, IntType(256))),
    ('merkle_root', IntType(256)),
    ('timestamp', IntType(32)),
    ('bits', FloatingIntegerType()),  # todo Check this new type
    ('nonce', IntType(32)),
])


# todo check it from bitcoin/data/py derivated from pack.Type class
class TransactionType(pack.Type):
    _int_type = pack.IntType(32)
    _varint_type = pack.VarIntType()
    _witness_type = pack.ListType(pack.VarStrType())
    _wtx_type = pack.ComposedType([
        ('flag', pack.IntType(8)),
        ('tx_ins', pack.ListType(tx_in_type)),
        ('tx_outs', pack.ListType(tx_out_type))
    ])
    _ntx_type = pack.ComposedType([
        ('tx_outs', pack.ListType(tx_out_type)),
        ('lock_time', _int_type)
    ])
    _write_type = pack.ComposedType([
        ('version', _int_type),
        ('marker', pack.IntType(8)),
        ('flag', pack.IntType(8)),
        ('tx_ins', pack.ListType(tx_in_type)),
        ('tx_outs', pack.ListType(tx_out_type))
    ])

    def read(self, file):
        version = self._int_type.read(file)
        marker = self._varint_type.read(file)
        if marker == 0:
            next = self._wtx_type.read(file)
            witness = [None]*len(next['tx_ins'])
            for i in xrange(len(next['tx_ins'])):  # todo replace by py3 range()
                witness[i] = self._witness_type.read(file)
            locktime = self._int_type.read(file)
            return dict(version=version, marker=marker, flag=next['flag'], tx_ins=next['tx_ins'], tx_outs=next['tx_outs'], witness=witness, lock_time=locktime)
        else:
            tx_ins = [None]*marker
            for i in xrange(marker):  # todo replace by py3 range()
                tx_ins[i] = tx_in_type.read(file)
            next = self._ntx_type.read(file)
            return dict(version=version, tx_ins=tx_ins, tx_outs=next['tx_outs'], lock_time=next['lock_time'])

    def write(self, file, item):
        if is_segwit_tx(item):
            assert len(item['tx_ins']) == len(item['witness'])
            self._write_type.write(file, item)
            for w in item['witness']:
                self._witness_type.write(file, w)
            self._int_type.write(file, item['lock_time'])
            return
        return tx_id_type.write(file, item)


tx_type = TransactionType()


class Address_Type():

    @staticmethod
    def parseIn(_data):
        # данные внутри других данных разделяются символом ","
        data = _data.split(',')
        return {'services': data[0], 'address': data[1], 'port': data[2]}

    @staticmethod
    def parseOut(_data):
        return str(_data['services']) + ' ' + str(_data['address']) + ' ' + str(_data['port'])

# class Bitcoin_Data_Address_Type(): # todo SAME as Address_Type()!!! check

#     @staticmethod
#     def parseIn(_data):
#         data = _data.split(',') #данные внутри других данных разделяются символом ","
#         pass

#     @staticmethod
#     def parseOut(_data):
#         pass


class Share_Type():

    @staticmethod
    def parseIn(_data):
        # данные внутри других данных разделяются символом ","
        data = _data.split(',')
        return {'type': data[0], 'contents': data[1]}

    @staticmethod
    def parseOut(_data):
        return str(_data['type'] + ' ' + str(_data['contents']))


class Block_Header_Type():

    @staticmethod
    def parseIn(_data):
        # данные внутри других данных разделяются символом ","
        data = _data.split(',')
        # ('version', pack.IntType(32)),
        # ('previous_block', pack.PossiblyNoneType(0, pack.IntType(256))),
        # ('merkle_root', pack.IntType(256)),
        # ('timestamp', pack.IntType(32)),
        # ('bits', FloatingIntegerType()),
        # ('nonce', pack.IntType(32)),
        return {'version': data[0], 'previous_block': data[1], 'merkle_root': data[2], 'timestamp': data[3], 'bits': data[4], 'nonce': data[5]}

    @staticmethod
    def parseOut(_data):
        return str(_data['version']) + ' ' + str(_data['previous_block']) + ' ' + str(_data['merkle_root']) + ' ' + str(_data['timestamp']) + ' ' + str(_data['bits']) + ' ' + str(_data['nonce'])


class TX_Type():  # todo check TransactionType class above @line #410

    @staticmethod
    def parseIn(_data):
        # данные внутри других данных разделяются символом ","
        data = _data.split(',')
        pass

    @staticmethod
    def parseOut(_data):
        pass


class UnpackResult:

    def __init__(self):
        self.res = ''

    def __iadd__(self, other):
        if isinstance(other, bytes):
            other = other.decode('utf-8')
        self.res += str(other) + ' '
        return self

    def __add__(self, other):
        self.res += other.res

    def __str__(self):
        return self.res


class msg:

    def pack(self, _data):
        data = self.parseVars(_data)
        return self._pack(data)

    def unpack(self, _data):
        data = _data
        if isinstance(_data, str):
            data = _data.encode('utf-8')
        return self._unpack(data)

    def parseVars(self, vars):
        # в c++ переменные в stringstream подаются с разделителим в виде символа ";".
        res = vars.split(';')
        return res  # список переменных на упаковку.


class messageError(msg):
    command = 'error'

    message_error = ComposedType([
        ('issue', VarStrType())
    ])

    def __init__(self, text):
        self.issue = text

    def _pack(self, data):
        if self.issue:
            msg_dict = {'issue': self.issue}
        else:
            msg_dict = {'issue': data}
        return self.message_error.pack(msg_dict)

    def _unpack(self, data):
        pass


class messageVersion(msg):
    command = 'version'

    message_version = ComposedType([
        ('version', IntType(32)),
        ('services', IntType(64)),
        ('addr_to', address_type),
        ('addr_from', address_type),
        ('nonce', IntType(64)),
        ('sub_version', VarStrType()),
        ('mode', IntType(32)),  # always 1 for legacy compatibility
        ('best_share_hash', PossiblyNoneType(0, IntType(256))),
    ])

    def _pack(self, data):
        msg_dict = {'version': int(data[0]),
                    'services': int(data[1]),
                    'addr_to': Address_Type.parseIn(data[2]),
                    'addr_from': Address_Type.parseIn(data[3]),
                    'nonce': int(data[4]),
                    'sub_version': data[5],
                    'mode': int(data[6]),
                    'best_share_hash': int(data[7])}  # int?

        return self.message_version.pack(msg_dict)

    def _unpack(self, data):
        res = UnpackResult()
        t = dict(self.message_version.unpack(data))
        res += t['version']
        res += t['services']

        res += Address_Type.parseOut(t['addr_to'])  # todo: test
        res += Address_Type.parseOut(t['addr_from'])  # todo: test

        res += t['nonce']
        res += t['sub_version']
        res += t['mode']
        res += t['best_share_hash']
        return res


class messagePing(msg):
    command = 'ping'

    message_ping = ComposedType([])

    def _pack(self, data):
        return self.message_ping.pack({})

    def _unpack(self, data):
        res = UnpackResult()
        t = dict(self.message_ping.unpack(data))
        res = ''
        return res


class messageAddrme(msg):
    command = 'addrme'

    message_addrme = ComposedType([('port', IntType(16))])

    def _pack(self, data):
        msg_dict = {'port': int(data[0])}
        return self.message_addrme.pack(msg_dict)

    def _unpack(self, data):
        res = UnpackResult()
        t = dict(self.message_addrme.unpack(data))
        res = t['port']
        return res


class messageAddrs(msg):
    command = 'addrs'

    message_addrs = ComposedType([
        ('addrs', ListType(ComposedType([
            ('timestamp', IntType(64)),
            ('address', address_type),  # todo check it out
        ]))),
    ])

    def _pack(self, data):
        msg_dict = {
            'addrs': [{
                'timestamp': int(data[0]),
                'address': Address_Type.parseIn(data[1]),
            }]}
        return self.message_addrs.pack(msg_dict)

    def _unpack(self, data):
        res = UnpackResult()
        t = dict(self.message_addrs.unpack(data))
        res += t['timestamp']
        res += t['address']  # todo check nested
        return res


class messageGetAddrs(msg):
    command = 'getaddrs'

    message_getaddrs = ComposedType([
        ('count', IntType(32)),
    ])

    def _pack(self, data):
        msg_dict = {
            'count': int(data[0]),
        }
        return self.message_getaddrs.pack(msg_dict)

    def _unpack(self, data):
        res = UnpackResult()
        t = dict(self.message_getaddrs.unpack(data))
        res = t['count']
        return res


class messageShares(msg):
    command = 'shares'

    message_shares = ComposedType([
        # todo check ('shares', pack.ListType(p2pool_data.share_type))
        ('shares', ListType(p2pool_data.share_type)),
    ])

    def _pack(self, data):
        msg_dict = {
            'shares': ListType(data[0]),
        }
        return self.message_shares.pack(msg_dict)

    def _unpack(self, data):
        res = UnpackResult()
        t = dict(self.message_shares.unpack(data))
        res = t['shares']
        return res


class messageShareReq(msg):
    command = 'sharereq'

    message_shrereq = ComposedType([
        ('id', IntType(256)),
        # todo check ('hashes', pack.ListType(pack.IntType(256)))
        ('hashes', ListType(IntType(256)))
        ('parents', VarIntType()),  # todo Var int type check
        # todo check ('stops', pack.ListType(pack.IntType(256)))
        ('stops', ListType(IntType(256)))
    ])

    def _pack(self, data):
        msg_dict = {
            'id': int(data[0]),
            'hashes': ListType(data[1]),
            'parents': VarIntType(data[2]),
            'stops': ListType(data[3]),
        }
        return self.message_shrereq.pack(msg_dict)\


    def _unpack(self, data):
        res = UnpackResult()
        t = dict(self.message_sharereq.unpack(data))
        res += t['id']
        res += t['hashes']
        res += t['parents']
        res += t['stops']
        return res


class messageShareReply(msg):
    command = 'sharereply'

    message_sharereply = ComposedType([
        ('id', IntType(256)),
        ('result', EnumType(VarIntType(), {0: 'good', 1: 'too long', 2: 'unk2',
                                           3: 'unk3', 4: 'unk4', 5: 'unk5', 6: 'unk6'})),  # todo enum & Var int type
        ('shares', ListType(p2pool_data.share_type)),  # todo share_type
    ])

    def _pack(self, data):
        msg_dict = {
            'id': int(data[0]),
            # todo chackout this
            'result': EnumType(VarIntType(data[1]), {0: 'good', 1: 'too long', 2: 'unk2', 3: 'unk3', 4: 'unk4', 5: 'unk5', 6: 'unk6'}),
            'shares': {parseShareType(data[2])},  # todo checkout this
        }
        return self.message_sharereply.pack(msg_dict)

    def _unpack(self, data):
        res = UnpackResult()
        t = dict(self.message_sharereply.unpack(data))
        res += t['id']
        res += t['result']
        res += t['shares']
        return res


class messageBestBlock(msg):
    command = 'bestblock'

    message_bestblock = ComposedType([
        ('header', block_header_type),  # todo block header type
    ])

    def _pack(self, data):
        msg_dict = {
            'header': parseBlock_header_type(data[0]),  # todo check this out
        }
        return self.message_bestblock.pack(msg_dict)

    def _unpack(self, data):
        res = UnpackResult()
        t = dict(self.message_bestblock.unpack(data))
        res = t['header']
        return res


class messageHaveTX(msg):
    command = 'have_tx'

    message_have_tx = ComposedType([
        ('tx_hashes', ListType(IntType(256))),
    ])

    def pack(self, data):
        msg_dict = {
            'tx_hashes': [IntType(256)]  # todo check this out
        }
        return self.message_have_tx.pack(msg_dict)

    def unpack(self, data):
        res = UnpackResult()
        t = dict(self.message_have_tx.unpack(data))
        res += t['tx_hashes']
        return res


class messageLosingTX(msg):
    command = 'losing_tx'

    message_losing_tx = ComposedType([
        ('tx_hashes', ListType(IntType(256))),  # todo check pack.
    ])

    def _pack(self, data):
        msg_dict = {
            'tx_hashes': [IntType(256)],
        }
        return self.message_losing_tx.pack(msg_dict)

    def _unpack(self, data):
        res = UnpackResult()
        t = dict(self.message_losing_tx.unpack(data))
        res += t['tx_hashes']
        return res


class messageRememberTX(msg):
    command = 'remember_tx'

    message_remember_tx = ComposedType([
        ('tx_hashes', ListType(IntType(256))),
        ('txs', ListType(bitcoin_data.tx_type)),
    ])

    def _pack(self, data):
        msg_dict = {
            'tx_hashes': [int(data[0])],
            'txs': [parseTX_type(data[1])],
        }
        return self.message_remember_tx.pack(msg_dict)

    def _unpack(self, data):
        res = UnpackResult()
        t = dict(self.message_remember_tx.unpack(data))
        res += t['tx_hashes']
        res += t['txs']
        return res


class messageForgetTX(msg):
    command = 'forget_tx'

    message_forget_tx = ComposedType([
        ('tx_hashes', ListType(IntType(256))),
    ])

    def _pack(self, data):
        msg_dict = {
            [int(data[0])],  # todo check list key
        }
        return self.message_forget_tx.pack(msg_dict)

    def _unpack(self, data):
        res = UnpackResult()
        t = dict(self.message_forget_tx.unpack(data))
        res += t['tx_hashes']
        return res
# ------------------------------------------packtypes-for-C---------------------------------


EnumMessages = {
    9999: messageError,  # todo: create this class
    0: messageVersion,
    1: messagePing,
    2: messageAddrme,
    3: messageAddrs,
    4: messageGetAddrs,
    5: messageShares,
    6: messageShareReq,
    7: messageShareReply,
    8: messageBestBlock,
    9: messageHaveTX,
    10: messageLosingTX,
    11: messageRememberTX,
    12: messageForgetTX,
}


def message_from_str(strcmd):
    for (k, v) in EnumMessages:
        if v.command == strcmd:
            return v
    return messageError('error str message')


def message_pack(command, vars):
    t = EnumMessages[command]
    return t.pack(vars)


def message_unpack(command, data):
    pass
 
def Receive(msg):
    command, checksum, payload = msg.split(' ')
    
    #remove
    if hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4] != checksum:
        return '-1'
    #------------


    type_ = EnumMessages[int(command)]
    if type_ is None:
        return '-2'
    
    return type_.unpack(payload) #todo: payload wanna bytes.

    #self.packetReceived(command, type_.unpack(payload, self.ignore_trailing_payload))

# ------------------------------------------TESTS------------------------------------------
def TEST_PACK_UNPACK():
    arrAddrs = [(1, 1), (2, 2), (3, 3), (4, 4), (5, 5)]

    addrs = [
        dict(
            timestamp=int(host+port),
            address=dict(
                services=host,
                address='192.168.1.1',
                port=port,),) for host, port in arrAddrs]

    test_message = ComposedType([
        ('version', IntType(32)),
        ('services', IntType(64)),
        ('sub_version', VarStrType()),
        ('best_share_hash', PossiblyNoneType(0, IntType(256))),
        ('addrs', ListType(ComposedType([
            ('timestamp', IntType(64)),
            ('address', address_type),
        ])))
    ])

    dict_test_message = {'version': 1,
                         'services': 2,
                         'sub_version': "STRING",
                         'best_share_hash': 3,
                         'addrs': addrs
                         }

    packed = test_message.pack(dict_test_message)
    print(packed)
    unpacked = test_message.unpack(packed)
    print(unpacked)
    print(dict(unpacked).values())


def TEST_SHA256():
    import hashlib
    data = 'As Bitcoin relies on 80 byte header hashes, we want to have an example for that.'.encode(
        'utf-8')
    print(hashlib.sha256(data).hexdigest() ==
          '7406e8de7d6e4fffc573daef05aefb8806e7790f55eab5576f31349743cca743')


def TEST_UNPACKRES():
    t = UnpackResult()

    t += 123
    t += 'test123test'
    t += (123, 'asd')
    t += {1: '23', '23': 1}

    print(t)

# TEST_SHA256()
# TEST_PACK_UNPACK()
# TEST_UNPACKRES()
