"""
================================= ha-addon-modbusspy ============================

Home assistant addon for sniffing a serial modbus RTU connection (modbus client)
All sniffed registers are exposed on the TCP modbus interface (modbus server)
Supports
  - holding register sniffing
  - input register sniffing
Author: @infradom
    License: MIT
Credits:
    This code is inspired by and derived from the work of Simon Hobbs
    Github: shhobbs/ModbusSniffer
    License: MIT
Async version by @infradon
Modbus TCP server interface by @infradom
Known problems:
    Not sure if serial_asyncio is fully supported on Windows
"""

#import serial
import json
import time

import asyncio
#import aioserial
import serial_asyncio

from pymodbus.factory import ClientDecoder
from pymodbus.factory import ServerDecoder
from pymodbus.transaction import ModbusRtuFramer
from pymodbus.version import version
from pymodbus.device import ModbusDeviceIdentification
from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusServerContext,
    ModbusSlaveContext,
    ModbusSparseDataBlock,
)
from pymodbus.server import (
    StartAsyncSerialServer,
    StartAsyncTcpServer,
    StartAsyncTlsServer,
    StartAsyncUdpServer,
)
from pymodbus.transaction import (
    ModbusAsciiFramer,
    ModbusRtuFramer,
    ModbusSocketFramer,
    ModbusTlsFramer,
)
#import pymodbus
#from pymodbus.transaction import ModbusRtuFramer
#from pymodbus.utilities import hexlify_packets
#from binascii import b2a_hex
from time import sleep
import sys
import logging
FORMAT = ('%(asctime)-15s %(threadName)-15s'
          ' %(levelname)-8s %(module)-15s:%(lineno)-8s %(message)s')

BLOCKSIZE = 256
RESYNC_GAP = 0.1 # required intermessage gap (s) prior to a resync attempt


hr_datablock = ModbusSequentialDataBlock.create()
ir_datablock = ModbusSequentialDataBlock.create()
co_datablock = ModbusSequentialDataBlock.create()
di_datablock = ModbusSequentialDataBlock.create()

#hr_datablock.setValues(1, [0x4833, 0x3454, 0x3038, 0x3030 , 0x3030, 0x3030, 0x3030, 0])

cur_hr_address = None
cur_hr_len = 0
cur_ir_address = None
cur_ir_len = 0

_logger = logging.getLogger(__name__) # JCO

class myModbusRtuFramer(ModbusRtuFramer):

    """ slightly modified ModbusRtuFramer class
    capable of reading both master and slave packets
    (alternates between request and response)
    """
    def myProcessIncomingPacket(
        self, data, unit, **kwargs
    ):  # pylint: disable=arguments-differ
        """Process new packet pattern.
        This takes in a new request packet, adds it to the current
        packet stream, and performs framing on it. That is, checks
        for complete messages, and once found, will process all that
        exist.  This handles the case when we read N + 1 or 1 // N
        messages at a time instead of 1.
        The processed and decoded messages are pushed to the callback
        function to process and send.
        :param data: The new packet data
        :param callback: The function to send results to
        :param unit: Process if unit id matches, ignore otherwise (could be a
               list of unit ids (server) or single unit id(client/server)
        :param kwargs:
        """
        oosync = False
        if not isinstance(unit, (list, tuple)):
            unit = [unit]
        self.addToFrame(data)
        single = kwargs.get("single", False)
        while True:
            if self.isFrameReady():
                _logger.debug(f"frame is ready: header: {self._header} frame: {self._buffer}")
                if self.checkFrame():
                    if self._validate_unit_id(unit, single):
                        self._process(self.callback)
                        self.toggleMode()
                    else:
                        header_txt = self._header["uid"]
                        txt = f"Not a valid unit id - {header_txt}, ignoring!!"
                        _logger.warning(txt)
                        self.resetFrame()
                        break
                else:
                    _logger.warning(f"Frame check failed, ignoring!! JCO {self._buffer}")
                    self.resetFrame()
                    oosync = True
                    break
            else:
                _logger.debug(f"Frame - not ready - buffer: {self._buffer}")
                break
        return oosync

    def setDecoders(self, request_decoder, response_decoder):
        self.request_decoder = request_decoder
        self.response_decoder = response_decoder
        self.decoder = self.request_decoder

    def setCallbacks(self, request_callback, response_callback):
        self.request_callback = request_callback
        self.response_callback = response_callback
        self.callback = self.request_callback

    def toggleMode(self):
        if self.decoder == self.request_decoder:  
            self.decoder = self.response_decoder
            self.callback = self.response_callback
        else: 
            self.decoder = self.request_decoder
            self.callback = self.request_callback

    def curMode(self):
        if self.decoder == self.request_decoder: return "request"
        else: return "response" 


class SerialSnooper(asyncio.Protocol):
    global cur_hr_address, cur_ir_address
    global cur_hr_len, cur_ir_len

    kMaxReadSize = 256
    kByteLength = 10
    def __init__(self):
        super().__init__()
        self._transport = None
        #self.connection = serial.Serial(port, baud, timeout=self.kByteLength*self.kMaxReadSize/float(baud))
        #self.connection = serial.Serial(port, baud, timeout = 3*10 / float(baud) ) #timeout=0.5 * self.kByteLength*(BLOCKSIZE+3)/float(baud))
        self.request_decoder = ServerDecoder() #JCO
        self.response_decoder = ClientDecoder() #JCO
        self.framer = myModbusRtuFramer(decoder=self.request_decoder) #JCO
        self.framer.setDecoders(self.request_decoder, self.response_decoder) #JCO
        self.framer.setCallbacks(self.master_packet_callback, self.slave_packet_callback) #JCO

        self.idle = 0 # unknown
        self.oosync = True # out of sync
        self.lastts = time.time()
        #JCO self.response_framer = myModbusRtuFramer(decoder=ClientDecoder())
        #JCO self.request_framer = myModbusRtuFramer(decoder=ServerDecoder())

    
    def connection_made(self, transport):
        self._transport = transport
        log.info(f"port opened {self._transport}")
        #self._transport.serial.rts = False
        #self._transport.write(b'Hello, World!\n')

    def data_received(self, data):
        #log.info(f"data received {repr(data)}")

        ts = time.time()
        if len(data):
            self.idle = 0
            self.oosync =  self.oosync and ((ts - self.lastts) < RESYNC_GAP)
            if self.oosync:
                log.info(f"ignoring out of sync data: {data[:10]}")
                #self.oosync = (ts - self.lastts) <0.3 #< 3*(1*10) / float(baud) 
            else:
                log.debug(f"potential start of frame or continuation data {data}")
                self.oosync = self.process(data)
            self.lastts = ts # time.time()
        else:
            log.warning("empty message")


    def connection_lost(self, exc):
        log.indo('serial port closed')
        self._transport.loop.stop()

    def pause_writing(self):
        log.info(f"pause writing - buffer size {self._transport.get_write_buffer_size()}")

    def resume_writing(self):
        log.info(f"resume writing - buffer size {self._transport.get_write_buffer_size()}")
    

    def master_packet_callback(self, *args, **kwargs):
        global cur_hr_len, cur_ir_len
        global cur_hr_address, cur_ir_address
        arg = 0
        log.debug(f"master packet callback args: {args} kwargs: {kwargs} ")
        for msg in args:
            log.debug(f"master packet callback: {msg}")
            func_name = str(type(msg)).split('.')[-1].strip("'><").replace("Request", "")
            t1 = f"Master-> ID: {msg.unit_id}, Function: {func_name}: {msg.function_code}"

            try:
                t2 = f"Address: 0x{msg.address:02x}"
                if msg.function_code == 3: 
                    cur_hr_address = msg.address
                    cur_hr_len = msg.count
                if msg.function_code == 4: 
                    cur_ir_address = msg.address
                    cur_ir_len = msg.count
            except AttributeError:
                t2 = ""
            try:
                t3 = f"Count: {msg.count}"
            except AttributeError:
                t3 = ""
            try:
                t4 = f"Data: {msg.values}"
            except AttributeError:
                t4 = ""
            arg += 1
            t5 = f'{arg}/{len(args)}'
            log.info(f"{t1} {t2} {t3} {t4} {t5}")


    def slave_packet_callback(self, *args, **kwargs):
        arg = 0
        log.debug(f"slave packet callback args: {args} kwargs: {kwargs} ")
        for msg in args:
            log.debug(f"slave packet callback: {msg}")
            func_name = str(type(msg)).split('.')[-1].strip("'><").replace("Request", "")
            t1 = f"Slave-> ID: {msg.unit_id}, Function: {func_name}: {msg.function_code}"
            try:
                t2 = f"Address: 0x{msg.address:02x}"
            except AttributeError:
                t2 = ""
            try:
                count = len(msg.registers)
                t3 = f"Count: {count}"
            except AttributeError:
                t3 = ""
            try:
                t4 = f"Data: {msg.registers}"
                if (msg.function_code == 3) and (cur_hr_len == count): 
                    hr_datablock.setValues(cur_hr_address+1, msg.registers)
                    log.info(f"hr datablock set: 0x{cur_hr_address:2x} {msg.registers}")
                if (msg.function_code == 4) and (cur_ir_len == count): 
                    ir_datablock.setValues(cur_ir_address+1, msg.registers)
                    log.info(f"ir datablock set: 0x{cur_ir_address:2x} {msg.registers}")
            except AttributeError:
                t4 = ""
            arg += 1
            t5 = f'{arg}/{len(args)}'
            log.info(f"{t1} {t2} {t3} {t4} {t5}")
        log.debug(f"slave callback kwargs {kwargs}")

    def read_raw(self, n=BLOCKSIZE):
        return self.connection.read(n)

    def process(self, data):
        oosync = False
        if len(data) <= 0:
            return
        try:
            log.debug(f"Checking as {self.framer.curMode()}")
            oosync = self.framer.myProcessIncomingPacket(data, unit=None, single=True)
            pass
        except (IndexError, TypeError,KeyError) as e:
            print(e)
            pass
        return oosync


    def read(self):
        self.process(self.read_raw())

""" ================== the Modbus Server ========================
    ============================================================="""

def setup_server(args):
    #global hr_datablock, ir_datablock, co_datablock, di_datablock
    context = {
                0x01: ModbusSlaveContext(
                    di=di_datablock,
                    co=co_datablock,
                    hr=hr_datablock,
                    ir=ir_datablock,
                ),
    }
    single = False
    args.context = ModbusServerContext(slaves=context, single=single)
    args.identity = ModbusDeviceIdentification(
        info_name={
            "VendorName": "infradom",
            "ProductCode": "modbusspy",
            "VendorUrl": "https://github.com/riptideio/pymodbus/",
            "ProductName": "ModbusSpy",
            "ModelName": "ModbusSpy",
            "MajorMinorRevision": version.short(),
        }
    )
    return args

async def run_async_server(args):
    """Run server."""
    txt = f"### start ASYNC server, listening on {TCPPORT}"
    log.info(txt)
    address = ("", TCPPORT) if TCPPORT else None
    server = await StartAsyncTcpServer(
            context=args.context,  # Data storage
            identity=args.identity,  # server identify
            # TBD host=
            # TBD port=
            address=address,  # listen address
            # custom_functions=[],  # allow custom handling
            framer=ModbusSocketFramer, #args.framer,  # The framer strategy to use
            # handler=None,  # handler for each session
            allow_reuse_address=True,  # allow the reuse of an address
            # ignore_missing_slaves=True,  # ignore request to a missing slave
            # broadcast_enable=False,  # treat unit_id 0 as broadcast address,
            # timeout=1,  # waiting time for request to complete
            # TBD strict=True,  # use strict timing, t1.5 for Modbus RTU
            # defer_start=False,  # Only define server do not activate
    )
    return server

""" ============================================================"""

class setup_args():
    server = True 
    description = "Run async modbus server"

def convert_to_wrds(value):
    if isinstance(value, str): # convert string to list of 16 bit words
        res = []
        wrd = None
        for ch in value:
            if wrd == None: wrd = ord(ch)
            else: 
                wrd = wrd*256 + ord(ch)
                res.append(wrd)
                wrd = None
        if wrd: res.append(wrd)
        value = res
    if isinstance(value, (list, tuple)):
        return value #hr_datablock.setValues(key+1, value)
    else: log.error(f'invalid static_holdings_json: expecting {{ "14" : [1, 2], "15": "AB" }} structure')

if __name__ == "__main__":
    logging.basicConfig(format=FORMAT)
    log = logging.getLogger()
    log.setLevel(logging.INFO)

    BAUD = 9600
    f = open("/data/options.json")
    config = json.load(f)
    BAUD = config['baud']
    DEVICE = config['device']
    LOGLEVEL = config['loglevel']
    RESYNC_GAP = config['resync_gap'] 
    TCPPORT = config['tcpport']
    STATIC_HOLDINGS = config['static_holdings_json']
    f.close()

    if LOGLEVEL == 'debug': log.setLevel(logging.DEBUG)
    if LOGLEVEL == 'warning': log.setLevel(logging.WARNING)
    if LOGLEVEL == 'info': log.setLevel(logging.INFO)

    # initialize static holding registers
    STATIC_HOLDINGS = json.loads(STATIC_HOLDINGS)
    for (key, value,) in STATIC_HOLDINGS.items():
        if key.isnumeric(): key = int(key)
        else: log.error(f'non-numeric key in static_holdings_json: expecting {{ "14" : [1, 2], "15": "AB" }} structure')
        value = convert_to_wrds(value)
        if value: hr_datablock.setValues(int(key)+1, value)

    log.info(f"device: {DEVICE}; baud: {BAUD}; tcp port: {TCPPORT}; resync gap: {RESYNC_GAP}s")
    run_args = setup_server( setup_args() )
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    coro = serial_asyncio.create_serial_connection(loop, SerialSnooper, DEVICE, baudrate=BAUD, timeout = 3*10 / float(BAUD) )
    transport, protocol = loop.run_until_complete(coro)
    loop.run_until_complete(run_async_server(run_args))
    loop.run_forever()
    loop.close()

    sys.exit(0)
       
    