from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath

import pytest

from n0te.plugin_discovery import (
    PluginDiscoveryError,
    PluginRootPlanner,
    PluginSearchRoot,
)


def _paths_for(roots, format_kind: str) -> set[str]:
    return {str(root.path) for root in roots if root.format_kind == format_kind}


def _sources_for(roots, format_kind: str) -> set[str]:
    return {root.source for root in roots if root.format_kind == format_kind}


def test_macos_standard_roots_are_format_specific_and_bounded() -> None:
    roots = PluginRootPlanner.standard_roots(
        os_family="MACOS",
        home="/Users/artist",
        environment={"UNRELATED": ""},
    )

    assert _paths_for(roots, "VST3") == {
        "/Users/artist/Library/Audio/Plug-Ins/VST3",
        "/Library/Audio/Plug-Ins/VST3",
        "/Network/Library/Audio/Plug-Ins/VST3",
    }
    assert _paths_for(roots, "AU") == {
        "/Users/artist/Library/Audio/Plug-Ins/Components",
        "/Library/Audio/Plug-Ins/Components",
        "/System/Library/Components",
    }
    assert _paths_for(roots, "CLAP") == {
        "/Users/artist/Library/Audio/Plug-Ins/CLAP",
        "/Library/Audio/Plug-Ins/CLAP",
    }
    assert _paths_for(roots, "AAX") == {
        "/Library/Application Support/Avid/Audio/Plug-Ins"
    }
    assert _paths_for(roots, "LV2") == set()
    assert _paths_for(roots, "LADSPA") == set()
    assert {root.source for root in roots} == {"STANDARD"}


def test_windows_standard_roots_use_explicit_windows_environment() -> None:
    roots = PluginRootPlanner.standard_roots(
        os_family="WINDOWS",
        home=r"C:\Users\artist",
        environment={
            "LOCALAPPDATA": r"D:\Local",
            "PROGRAMFILES": r"C:\Program Files",
            "COMMONPROGRAMFILES": r"C:\Program Files\Common Files",
            "PROGRAMFILES(X86)": r"C:\Program Files (x86)",
            "COMMONPROGRAMFILES(X86)": r"C:\Program Files (x86)\Common Files",
        },
    )

    assert _paths_for(roots, "VST3") == {
        r"D:\Local\Programs\Common\VST3",
        r"C:\Program Files\Common Files\VST3",
        r"C:\Program Files (x86)\Common Files\VST3",
    }
    assert _paths_for(roots, "CLAP") == {
        r"C:\Program Files\Common Files\CLAP",
        r"D:\Local\Programs\Common\CLAP",
    }
    assert _paths_for(roots, "AAX") == {
        r"C:\Program Files\Common Files\Avid\Audio\Plug-Ins"
    }
    assert _paths_for(roots, "AU") == set()
    assert _paths_for(roots, "LV2") == set()
    assert _paths_for(roots, "LADSPA") == set()


def test_linux_environment_paths_replace_only_their_format_defaults() -> None:
    roots = PluginRootPlanner.standard_roots(
        os_family="LINUX",
        home="/home/artist",
        environment={
            "LV2_PATH": "/opt/lv2:/srv/lv2",
            "LADSPA_PATH": "/opt/ladspa",
        },
    )

    assert _paths_for(roots, "LV2") == {"/opt/lv2", "/srv/lv2"}
    assert _sources_for(roots, "LV2") == {"ENVIRONMENT"}
    assert _paths_for(roots, "LADSPA") == {"/opt/ladspa"}
    assert _sources_for(roots, "LADSPA") == {"ENVIRONMENT"}
    assert "/home/artist/.lv2" not in _paths_for(roots, "LV2")
    assert "/usr/lib/lv2" not in _paths_for(roots, "LV2")
    assert "/usr/lib/ladspa" not in _paths_for(roots, "LADSPA")

    assert _paths_for(roots, "VST3") == {
        "/home/artist/.vst3",
        "/usr/local/lib/vst3",
        "/usr/lib/vst3",
    }
    assert _paths_for(roots, "CLAP") == {
        "/home/artist/.clap",
        "/usr/local/lib/clap",
        "/usr/lib/clap",
    }
    assert _paths_for(roots, "AU") == set()
    assert _paths_for(roots, "AAX") == set()


def test_blank_relevant_environment_values_fall_back_to_standard_roots() -> None:
    roots = PluginRootPlanner.standard_roots(
        os_family="LINUX",
        home="/home/artist",
        environment={
            "LV2_PATH": "   ",
            "LADSPA_PATH": "",
            "UNRELATED_EMPTY": "",
            "UNRELATED_NON_TEXT": object(),
        },
    )

    assert _paths_for(roots, "LV2") == {
        "/home/artist/.lv2",
        "/usr/local/lib/lv2",
        "/usr/lib/lv2",
    }
    assert _sources_for(roots, "LV2") == {"STANDARD"}
    assert _paths_for(roots, "LADSPA") == {
        "/usr/local/lib/ladspa",
        "/usr/lib/ladspa",
    }
    assert _sources_for(roots, "LADSPA") == {"STANDARD"}


def test_relevant_environment_values_remain_strict_text() -> None:
    with pytest.raises(TypeError, match="LV2_PATH"):
        PluginRootPlanner.standard_roots(
            os_family="LINUX",
            home="/home/artist",
            environment={"LV2_PATH": 42},
        )

    with pytest.raises(TypeError, match="environment variable names"):
        PluginRootPlanner.standard_roots(
            os_family="LINUX",
            home="/home/artist",
            environment={7: "/opt/lv2"},
        )


def test_environment_search_paths_must_be_absolute_and_are_deduplicated() -> None:
    with pytest.raises(PluginDiscoveryError, match="absolute"):
        PluginRootPlanner.standard_roots(
            os_family="LINUX",
            home="/home/artist",
            environment={"LV2_PATH": "/opt/lv2:relative/lv2"},
        )

    roots = PluginRootPlanner.standard_roots(
        os_family="LINUX",
        home="/home/artist",
        environment={"LV2_PATH": "/opt/lv2:/opt/lv2"},
    )
    assert _paths_for(roots, "LV2") == {"/opt/lv2"}
    assert len([root for root in roots if root.format_kind == "LV2"]) == 1


def test_windows_root_deduplication_is_case_insensitive() -> None:
    first = PluginSearchRoot(
        os_family="WINDOWS",
        format_kind="VST3",
        path=PureWindowsPath(r"C:\Program Files\Common Files\VST3"),
        source="STANDARD",
    )
    second = PluginSearchRoot(
        os_family="WINDOWS",
        format_kind="VST3",
        path=PureWindowsPath(r"c:\PROGRAM FILES\COMMON FILES\vst3"),
        source="EXPLICIT",
    )

    combined = PluginRootPlanner.combine((first,), (second,))

    assert combined == (first,)


def test_explicit_custom_root_is_evidence_input_only_not_persistence() -> None:
    root = PluginSearchRoot.explicit(
        os_family="MACOS",
        format_kind="VST3",
        path="/Volumes/Audio/Third Party/VST3",
    )

    assert root.source == "EXPLICIT"
    assert root.path == PurePosixPath("/Volumes/Audio/Third Party/VST3")
    assert not hasattr(root, "persisted")
    assert not hasattr(root, "licensed")
    assert not hasattr(root, "hostable")


def test_format_support_is_not_flattened_across_operating_systems() -> None:
    mac = PluginRootPlanner.standard_roots(os_family="MACOS", home="/Users/a")
    windows = PluginRootPlanner.standard_roots(
        os_family="WINDOWS", home=r"C:\Users\a"
    )
    linux = PluginRootPlanner.standard_roots(os_family="LINUX", home="/home/a")

    assert {item.format_kind for item in mac} == {"VST3", "AU", "AAX", "CLAP"}
    assert {item.format_kind for item in windows} == {"VST3", "AAX", "CLAP"}
    assert {item.format_kind for item in linux} == {
        "VST3",
        "CLAP",
        "LV2",
        "LADSPA",
    }


def test_planner_rejects_unsupported_os_and_non_text_paths() -> None:
    with pytest.raises(PluginDiscoveryError, match="unsupported discovery os_family"):
        PluginRootPlanner.standard_roots(os_family="HAIKU", home="/home/a")

    with pytest.raises(TypeError, match="path-like"):
        PluginSearchRoot.explicit(
            os_family="LINUX",
            format_kind="VST3",
            path=True,
        )
