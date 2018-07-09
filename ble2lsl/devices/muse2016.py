"""Interfacing parameters for the Muse headband (2016 version).

More information on the data provided by the Muse 2016 headband can be found
at `Available Data - Muse Direct`_

TODO:
    * Figure out maximum string size for status messages, or split into fields
      (can't send dict over LSL)
    * return standard acceleration units and not g's...
    * verify telemetry and IMU conversions and units
    * DRL/REF characteristic
    * verify sample ID is chunk ID
    * don't use lambdas for CONVERT_FUNCS?

.. _Available Data - Muse Direct:
   http://developer.choosemuse.com/tools/windows-tools/available-data-muse-direct
"""

from ble2lsl.devices.device import BasePacketHandler
from ble2lsl.utils import dict_partial_from_keys

import ast

import bitstring
import numpy as np
from pygatt import BLEAddressType

STREAMS = ['eeg', 'accelerometer', 'gyroscope', 'telemetry', 'status']
"""Data provided by the Muse 2016 headset, and available for subscription
over BLE."""

# for constructing dicts with STREAMS as keys
streams_dict = dict_partial_from_keys(STREAMS)

PARAMS = dict(
    manufacturer='Interaxon',
    name='Muse',
    streams=dict(
        type=streams_dict(['EEG', 'ACC', 'GYR', 'TLM', 'STAT']),  # XDF
        channel_count=streams_dict([5, 3, 3, 4, 1]),
        nominal_srate=streams_dict([256, 52, 52, 0.1, None]),
        channel_format=streams_dict(['float32', 'float32', 'float32',
                                     'float32', 'string']),
        numpy_dtype=streams_dict(['float32', 'float32', 'float32', 'float32',
                                  'object']),
        units=streams_dict([('uV',) * 5,
                            ('milli-g',) * 3,
                            ('deg/s',) * 3,
                            ('%', '?/mV', '?/mV', 'C'),
                            ('',)]),
        ch_names=streams_dict([('TP9', 'AF7', 'AF8', 'TP10', 'Right AUX'),
                               ('x', 'y', 'z'),
                               ('x', 'y', 'z'),
                               ('battery', 'fuel_gauge', 'adc_volt',
                                'temperature'),
                               ('message')]),
        chunk_size=streams_dict([12, 3, 3, 1, 1]),
    ),
    ble=dict(
        address_type=BLEAddressType.public,
        interval_min=60,  # pygatt default, seems fine
        interval_max=76,  # pygatt default

        # characteristic UUIDs
        eeg=['273e0003-4c4d-454d-96be-f03bac821358',
             '273e0004-4c4d-454d-96be-f03bac821358',
             '273e0005-4c4d-454d-96be-f03bac821358',
             '273e0006-4c4d-454d-96be-f03bac821358',
             '273e0007-4c4d-454d-96be-f03bac821358'],
        # reference='273e0008-4c4d-454d-96be-f03bac821358',
        accelerometer='273e000a-4c4d-454d-96be-f03bac821358',
        gyroscope='273e0009-4c4d-454d-96be-f03bac821358',
        telemetry='273e000b-4c4d-454d-96be-f03bac821358',
        status='273e0001-4c4d-454d-96be-f03bac821358',  # same as send
        send='273e0001-4c4d-454d-96be-f03bac821358',

        # commands (write to send characteristic)
        stream_on=(0x02, 0x64, 0x0a),  # b'd'
        stream_off=(0x02, 0x68, 0x0a),  # ?
        # keep_alive=(0x02, 0x6b, 0x0a), # (?) b'k'
        # request_info=(0x03, 0x76, 0x31, 0x0a),
        # request_status=(0x02, 0x73, 0x0a),
        # reset=(0x03, 0x2a, 0x31, 0x0a)
    ),
)
"""Muse headset parameters, including BLE characteristics."""

HANDLE_NAMES = {14: "status", 26: "telemetry", 23: "accelerometer",
                20: "gyroscope", 32: "eeg", 35: "eeg", 38: "eeg", 41: "eeg",
                44: "eeg"}
"""Stream name associated with each packet handle."""

PACKET_FORMATS = streams_dict(['uint:16' + ',uint:12' * PARAMS["chunk_size"],
                               'uint:16' + ',int:16' * 9,
                               'uint:16' + ',int:16' * 9,
                               'uint:16' + ',uint:16' * 4,
                               ','.join(['uint:8'] * 20)])
"""Byte formats of the incoming packets."""

CONVERT_FUNCS = streams_dict([lambda data: 0.48828125 * (data - 2048),
                              lambda data: 0.0000610352 * data.reshape((3, 3)),
                              lambda data: 0.0074768 * data.reshape((3, 3)),
                              lambda data: np.array([data[0] / 512,
                                                     2.2 * data[1],
                                                     data[2], data[3]]),
                              lambda data: None])
"""Functions to render unpacked data into the appropriate shape and units."""

EEG_HANDLE_CH_IDXS = {32: 0, 35: 1, 38: 2, 41: 3, 44: 4}
EEG_HANDLE_RECEIVE_ORDER = [44, 41, 38, 32, 35]
"""Channel indices and receipt order of EEG packets."""


class PacketHandler(BasePacketHandler):
    """Process packets from the Muse 2016 headset into chunks."""

    def __init__(self, callback, subscriptions, **kwargs):
        super().__init__(device_params=PARAMS, callback=callback,
                         subscriptions=subscriptions, **kwargs)
        self._message = ""

    def process_packet(self, handle, packet):
        """Unpack, convert, and return packet contents."""
        name = HANDLE_NAMES[handle]
        unpacked = _unpack(packet, PACKET_FORMATS[name])

        if name not in self._subscriptions:
            return

        if name == "status":
            self._process_status(unpacked)
        else:
            data = np.array(unpacked[1:], dtype=PARAMS["numpy_dtypes"][name])

        if name == "eeg":
            idx = EEG_HANDLE_CH_IDXS[handle]
            self._sample_idxs[name][idx] = unpacked[0]
            self._chunks[name][idx] = CONVERT_FUNCS[name](data)
            if handle == EEG_HANDLE_RECEIVE_ORDER[-1]:
                self._callback(name, self._sample_idxs[name],
                               self._chunks[name])
        else:
            self._sample_idxs[name] = unpacked[0]
            self._chunks[name][:, :] = CONVERT_FUNCS[name](data)
            self._callback(name, self._sample_idxs[name],
                           self._chunks[name])

    def _process_status(self, unpacked):
        message_chars = [chr(i) for i in unpacked[1:]]
        status_message_partial = "".join(message_chars)[:unpacked[0]]
        self._message += status_message_partial
        if status_message_partial[-1] == '}':
            self._message = self._message.replace('\n', '')
            # parse and enqueue dict
            self._callback("status", -1, ast.literal_eval(self._message))
            self._message = ""


def _unpack(packet, packet_format):
    packet_bits = bitstring.Bits(bytes=packet)
    unpacked = packet_bits.unpack(packet_format)
    return unpacked
