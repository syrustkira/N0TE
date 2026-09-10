-- N0TE REAPER read-side bridge.
-- Observes the active project and writes bounded local snapshots only.
-- It does not call any REAPER setter/action API and never emits the .rpp path.

local SCHEMA = "n0te.reaper-observation/v1"
local ADAPTER_ID = "n0te-reaper-reascript"
local ADAPTER_VERSION = "1"
local WRITE_INTERVAL = 0.25
local MAX_SELECTED_TRACKS = 32

local resource_path = reaper.GetResourcePath()
local bridge_dir = resource_path .. "/Scripts/N0TEBridge"
local snapshot_path = bridge_dir .. "/n0te_snapshot.json"
local temp_path = bridge_dir .. "/.n0te_snapshot.tmp"
local last_write = 0.0
local last_project_key = nil
local bridge_session_id = nil

reaper.RecursiveCreateDirectory(bridge_dir, 0)

local function json_escape(value)
  local text = tostring(value or "")
  text = text:gsub("\\", "\\\\")
  text = text:gsub('"', '\\"')
  text = text:gsub("\b", "\\b")
  text = text:gsub("\f", "\\f")
  text = text:gsub("\n", "\\n")
  text = text:gsub("\r", "\\r")
  text = text:gsub("\t", "\\t")
  return '"' .. text .. '"'
end

local function bool_json(value)
  return value and "true" or "false"
end

local function rotate_session()
  local guid = reaper.genGuid("")
  guid = tostring(guid):gsub("[{}]", "")
  bridge_session_id = "reaper-" .. guid:lower()
end

local function runtime_labels()
  local version = tostring(reaper.GetAppVersion())
  local os_token = tostring(reaper.GetOS())
  local lower = version:lower()
  local os_name = "Other"
  local machine = "unknown"

  if os_token == "Win32" then
    os_name = "Windows"
    machine = "x86"
  elseif os_token == "Win64" then
    os_name = "Windows"
    machine = lower:find("arm64", 1, true) and "arm64" or "x86_64"
  elseif os_token == "macOS-arm64" then
    os_name = "Darwin"
    machine = "arm64"
  elseif os_token == "OSX64" then
    os_name = "Darwin"
    machine = "x86_64"
  elseif lower:find("linux%-x86_64") then
    os_name = "Linux"
    machine = "x86_64"
  elseif lower:find("linux%-aarch64") then
    os_name = "Linux"
    machine = "aarch64"
  elseif lower:find("linux%-armv7") then
    os_name = "Linux"
    machine = "armv7l"
  end
  return version, os_name, machine
end

local function selected_tracks_json(project)
  local count = reaper.CountSelectedTracks(project)
  local limit = math.min(count, MAX_SELECTED_TRACKS)
  local rows = {}
  for index = 0, limit - 1 do
    local track = reaper.GetSelectedTrack(project, index)
    if track then
      local ok, name = reaper.GetTrackName(track)
      if not ok or not name or name == "" then name = "Track" end
      local track_number = math.floor(reaper.GetMediaTrackInfo_Value(track, "IP_TRACKNUMBER") or 0)
      local track_index = math.max(0, track_number - 1)
      local guid = tostring(reaper.GetTrackGUID(track) or "")
      rows[#rows + 1] = "{" ..
        '"index":' .. tostring(track_index) .. "," ..
        '"guid":' .. json_escape(guid) .. "," ..
        '"name":' .. json_escape(name) ..
        "}"
    end
  end
  return "[" .. table.concat(rows, ",") .. "]", count > MAX_SELECTED_TRACKS
end

local function build_snapshot()
  local project, project_filename = reaper.EnumProjects(-1, "")
  if not project then return nil end

  local project_key = tostring(project) .. "\0" .. tostring(project_filename or "")
  if project_key ~= last_project_key or bridge_session_id == nil then
    last_project_key = project_key
    rotate_session()
  end

  local project_name = tostring(reaper.GetProjectName(project) or "")
  if project_name == "" then project_name = "Untitled" end
  local saved = project_filename ~= nil and tostring(project_filename) ~= ""
  local state_change_count = math.max(0, math.floor(reaper.GetProjectStateChangeCount(project) or 0))
  local tempo = tonumber(reaper.Master_GetTempo()) or 120.0
  local play_state = math.floor(reaper.GetPlayStateEx(project) or 0)
  local play_position = tonumber(reaper.GetPlayPositionEx(project)) or 0.0
  local repeat_enabled = (reaper.GetSetRepeatEx(project, -1) or 0) == 1
  local track_count = math.max(0, math.floor(reaper.CountTracks(project) or 0))
  local selected_json, selection_truncated = selected_tracks_json(project)
  local version, os_name, machine = runtime_labels()

  return "{" ..
    '"schema":' .. json_escape(SCHEMA) .. "," ..
    '"adapter":{"id":' .. json_escape(ADAPTER_ID) .. ',"version":' .. json_escape(ADAPTER_VERSION) .. "}," ..
    '"bridge_session_id":' .. json_escape(bridge_session_id) .. "," ..
    '"observed_at_epoch_seconds":' .. tostring(os.time()) .. "," ..
    '"runtime":{"host_family":"REAPER","version":' .. json_escape(version) .. ',"edition":"REAPER","os_name":' .. json_escape(os_name) .. ',"machine":' .. json_escape(machine) .. "}," ..
    '"project":{"name":' .. json_escape(project_name) .. ',"saved":' .. bool_json(saved) .. ',"state_change_count":' .. tostring(state_change_count) .. "}," ..
    '"tempo_bpm":' .. string.format("%.6f", tempo) .. "," ..
    '"transport":{"play_state":' .. tostring(play_state) .. ',"play_position_seconds":' .. string.format("%.6f", play_position) .. ',"repeat_enabled":' .. bool_json(repeat_enabled) .. "}," ..
    '"track_count":' .. tostring(track_count) .. "," ..
    '"selected_tracks":' .. selected_json .. "," ..
    '"selection_truncated":' .. bool_json(selection_truncated) ..
    "}"
end

local function write_snapshot(payload)
  local handle = io.open(temp_path, "wb")
  if not handle then return end
  handle:write(payload)
  handle:flush()
  handle:close()
  os.remove(snapshot_path)
  os.rename(temp_path, snapshot_path)
end

local function loop()
  local now = reaper.time_precise()
  if now - last_write >= WRITE_INTERVAL then
    last_write = now
    local payload = build_snapshot()
    if payload then write_snapshot(payload) end
  end
  reaper.defer(loop)
end

rotate_session()
loop()
