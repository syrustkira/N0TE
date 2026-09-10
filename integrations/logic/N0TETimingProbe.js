/*
N0TE Logic Timing Probe

Install this script in Logic Pro's Scripter MIDI FX on a dedicated monitor
instrument strip. Route that strip's External Instrument MIDI Destination to
"Logic Pro Virtual Out". N0TE listens to that CoreMIDI port outside Logic.

This probe is intentionally read-only. It reports only TimingInfo fields that
Logic's documented Scripter API exposes. It does not inspect or modify project
files, tracks, plug-ins, regions, automation, tempo, transport, or routing.
*/

var NeedsTimingInfo = true;

var N0TE_CHANNEL = 16;
var N0TE_PROTOCOL_VERSION = 1;
var CC_SESSION_0 = 102;
var CC_SESSION_1 = 103;
var CC_SESSION_2 = 104;
var CC_SESSION_3 = 105;
var CC_TEMPO_MSB = 106;
var CC_TEMPO_LSB = 107;
var CC_FLAGS = 108;
var CC_METER_NUMERATOR = 109;
var CC_METER_DENOMINATOR = 110;
var CC_END = 116;
var CC_CHECKSUM = 117;
var CC_VERSION = 118;
var CC_START = 119;

var _sequence = 0;
var _sessionToken = Math.floor(Math.random() * 0x10000000);
var _lastSignature = "";

function _clamp7(value) {
    value = Math.round(Number(value));
    if (value < 0) return 0;
    if (value > 127) return 127;
    return value;
}

function _sendCC(number, value) {
    var event = new ControlChange();
    event.channel = N0TE_CHANNEL;
    event.number = number;
    event.value = _clamp7(value);
    event.send();
}

function _sessionBytes() {
    return [
        (_sessionToken >> 21) & 0x7f,
        (_sessionToken >> 14) & 0x7f,
        (_sessionToken >> 7) & 0x7f,
        _sessionToken & 0x7f
    ];
}

function _checksum(values) {
    var result = 0;
    for (var index = 0; index < values.length; index++) {
        result = (result ^ (values[index] & 0x7f)) & 0x7f;
    }
    return result;
}

function _emitTiming(info) {
    _sequence = (_sequence + 1) & 0x7f;

    var session = _sessionBytes();
    var tempo10 = Math.round(Number(info.tempo) * 10.0);
    if (tempo10 < 0) tempo10 = 0;
    if (tempo10 > 0x3fff) tempo10 = 0x3fff;
    var tempoMSB = (tempo10 >> 7) & 0x7f;
    var tempoLSB = tempo10 & 0x7f;
    var flags = (info.playing ? 1 : 0) | (info.cycling ? 2 : 0);
    var numerator = _clamp7(info.meterNumerator);
    var denominator = _clamp7(info.meterDenominator);

    var protectedValues = [
        _sequence,
        N0TE_PROTOCOL_VERSION,
        session[0], session[1], session[2], session[3],
        tempoMSB, tempoLSB,
        flags,
        numerator,
        denominator
    ];
    var checksum = _checksum(protectedValues);

    _sendCC(CC_START, _sequence);
    _sendCC(CC_VERSION, N0TE_PROTOCOL_VERSION);
    _sendCC(CC_SESSION_0, session[0]);
    _sendCC(CC_SESSION_1, session[1]);
    _sendCC(CC_SESSION_2, session[2]);
    _sendCC(CC_SESSION_3, session[3]);
    _sendCC(CC_TEMPO_MSB, tempoMSB);
    _sendCC(CC_TEMPO_LSB, tempoLSB);
    _sendCC(CC_FLAGS, flags);
    _sendCC(CC_METER_NUMERATOR, numerator);
    _sendCC(CC_METER_DENOMINATOR, denominator);
    _sendCC(CC_CHECKSUM, checksum);
    _sendCC(CC_END, _sequence);
}

function ProcessMIDI() {
    var info = GetTimingInfo();
    var beatBucket = Math.floor(Number(info.blockStartBeat));
    var tempo10 = Math.round(Number(info.tempo) * 10.0);
    var signature = [
        beatBucket,
        tempo10,
        info.playing ? 1 : 0,
        info.cycling ? 1 : 0,
        Math.round(Number(info.meterNumerator)),
        Math.round(Number(info.meterDenominator))
    ].join(":");

    if (signature !== _lastSignature) {
        _lastSignature = signature;
        _emitTiming(info);
    }
}

// The dedicated monitor strip is not a musical pass-through. Incoming events
// are intentionally consumed so only N0TE protocol traffic reaches Virtual Out.
function HandleMIDI(event) {
}
