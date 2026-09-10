# name=N0TEBridge
from __future__ import annotations

import json
import os
import platform
import time
import uuid

import channels
import general
import mixer
import transport
import ui

SCHEMA = "n0te.fl-studio-observation/v1"
ADAPTER_ID = "N0TEBridge"
ADAPTER_VERSION = "1"
SNAPSHOT_FILE_NAME = "n0te_snapshot.json"
WRITE_INTERVAL_SECONDS = 0.75

_BRIDGE_SESSION_ID = uuid.uuid4().hex
_SNAPSHOT_PATH = os.path.join(os.path.dirname(__file__), SNAPSHOT_FILE_NAME)
_LAST_WRITE_MONOTONIC = 0.0


def _call(fn, default=None, *args):
    try:
        return fn(*args)
    except Exception:
        return default


def _version_string():
    parts = []
    for mode in (0, 1, 2, 3):
        value = _call(ui.getVersion, None, mode)
        if value is None:
            break
        parts.append(str(value))
    return ".".join(parts) if parts else str(_call(ui.getVersion, "UNKNOWN", 5))


def _edition_string():
    value = _call(ui.getVersion, "UNKNOWN", 4)
    text = str(value).strip()
    return text or "UNKNOWN"


def _project_title():
    getter = getattr(general, "getProjectTitle", None)
    value = _call(getter, "Untitled") if getter is not None else "Untitled"
    text = str(value).strip()
    return text or "Untitled"


def _project_changed_flag():
    value = _call(general.getChangedFlag, 0)
    try:
        value = int(value)
    except Exception:
        value = 0
    return value if value in (0, 1, 2) else 0


def _selected_mixer_track():
    index = _call(mixer.trackNumber, -1)
    try:
        index = int(index)
    except Exception:
        return None
    if index < 0:
        return None
    name = str(_call(mixer.getTrackName, "Mixer %d" % index, index)).strip()
    return {"index": index, "name": name or "Mixer %d" % index}


def _selected_channel():
    index = _call(channels.selectedChannel, -1, 1, 0, 1)
    try:
        index = int(index)
    except Exception:
        return None
    if index < 0:
        return None
    name = str(_call(channels.getChannelName, "Channel %d" % index, index, True)).strip()
    return {"index": index, "name": name or "Channel %d" % index}


def _active_window():
    form_id = _call(ui.getFocusedFormID, -1)
    caption = str(_call(ui.getFocusedFormCaption, "")).strip()
    plugin = str(_call(ui.getFocusedPluginName, "")).strip()
    try:
        form_id = int(form_id)
    except Exception:
        form_id = -1
    if not caption:
        return None
    return {
        "form_id": form_id,
        "caption": caption,
        "plugin_name": plugin or None,
    }


def _snapshot():
    tempo = _call(mixer.getCurrentTempo, 120.0)
    position = _call(transport.getSongPos, 0.0)
    try:
        tempo = float(tempo)
    except Exception:
        tempo = 120.0
    try:
        position = float(position)
    except Exception:
        position = 0.0
    position = max(0.0, min(1.0, position))

    loop_mode = _call(transport.getLoopMode, 1)
    try:
        loop_mode = int(loop_mode)
    except Exception:
        loop_mode = 1
    if loop_mode not in (0, 1):
        loop_mode = 1

    return {
        "schema": SCHEMA,
        "adapter": {"id": ADAPTER_ID, "version": ADAPTER_VERSION},
        "bridge_session_id": _BRIDGE_SESSION_ID,
        "runtime": {
            "host_family": "FL_STUDIO",
            "version": _version_string(),
            "edition": _edition_string(),
            "os_name": platform.system() or "Unknown",
            "machine": platform.machine() or "unknown",
        },
        "observed_at_epoch_seconds": int(time.time()),
        "project": {
            "title": _project_title(),
            "changed_flag": _project_changed_flag(),
        },
        "tempo_bpm": tempo,
        "transport": {
            "is_playing": bool(_call(transport.isPlaying, 0)),
            "song_position": position,
            "loop_mode": loop_mode,
        },
        "selected_mixer_track": _selected_mixer_track(),
        "selected_channel": _selected_channel(),
        "active_window": _active_window(),
    }


def _write_snapshot(force=False):
    global _LAST_WRITE_MONOTONIC
    now = time.monotonic()
    if not force and now - _LAST_WRITE_MONOTONIC < WRITE_INTERVAL_SECONDS:
        return
    payload = _snapshot()
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    temp = _SNAPSHOT_PATH + ".tmp-" + _BRIDGE_SESSION_ID
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, _SNAPSHOT_PATH)
        _LAST_WRITE_MONOTONIC = now
    except Exception:
        try:
            if os.path.exists(temp):
                os.remove(temp)
        except Exception:
            pass


def _remove_own_snapshot():
    try:
        with open(_SNAPSHOT_PATH, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if payload.get("bridge_session_id") == _BRIDGE_SESSION_ID:
            os.remove(_SNAPSHOT_PATH)
    except Exception:
        pass


def OnInit():
    _write_snapshot(True)


def OnIdle():
    _write_snapshot(False)


def OnRefresh(flags):
    _write_snapshot(True)


def OnDoFullRefresh():
    _write_snapshot(True)


def OnProjectLoad(status):
    _write_snapshot(True)


def OnDeInit():
    _remove_own_snapshot()
