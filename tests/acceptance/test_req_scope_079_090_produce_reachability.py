from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest

from n0te.consumer_shell import ConsumerShell
from n0te.focus import FocusDimension, FocusUncertainError
from n0te.hosts import HostRuntimeIdentity
from n0te.instance import ProcessIdentity
from n0te.memory import HeadquartersMemory
from n0te.musical_plan import CompiledHostAction, HostCompilation
from n0te.platforms import PlatformEnvironment
from n0te.produce_shell import (
    ProduceRoute,
    _post_execute,
    _prepare_preview,
    _preview_token,
    bind_produce_route_provider,
)
from n0te.transactions import (
    CompensationResult,
    PostconditionResult,
    StepExecution,
    TransactionReceipt,
    TransactionSnapshot,
)


class Probe:
    def status(self, process: ProcessIdentity) -> str:
        return "UNKNOWN"


def process() -> ProcessIdentity:
    return ProcessIdentity.from_start_token(
        PlatformEnvironment.from_runtime_labels("Linux", "x86_64"),
        pid=79090,
        start_token="req-scope-079-090-produce-reachability",
    )


class AcceptanceCompiler:
    compiler_id = "acceptance-ableton-produce"
    compiler_version = "1"
    host_family = "ABLETON_LIVE"

    def compile(self, plan, runtime):
        return HostCompilation(
            plan_id=plan.plan_id,
            plan_fingerprint=plan.fingerprint,
            compiler_id=self.compiler_id,
            compiler_version=self.compiler_version,
            host_family=runtime.family,
            host_runtime_fingerprint=runtime.fingerprint,
            actions=(
                CompiledHostAction(
                    action_id="raise-bass-level",
                    route_kind="HOST_NATIVE",
                    capability="track.level.set",
                    payload_ref="acceptance:payload:raise-bass-level",
                    postcondition_ref="verify:track:bass:level",
                    compensatable=True,
                ),
            ),
            evidence_ref="acceptance:compiler:ableton:1",
        )


class RecordingDriver:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.transaction_id: str | None = None

    def prepare_snapshot(self, transaction_plan, musical_plan, compilation):
        self.events.append(("snapshot", transaction_plan.transaction_id))
        return TransactionSnapshot(
            transaction_plan.transaction_id,
            transaction_plan.operation_id,
            "acceptance:snapshot:produce",
            "sha256:acceptance-produce-snapshot",
            "acceptance:evidence:snapshot",
        )

    def execute_action(self, action):
        self.events.append(("execute", action.action_id))
        return StepExecution(
            action.action_id,
            "SUCCEEDED",
            "APPLIED",
            f"acceptance:evidence:execute:{action.action_id}",
            f"sha256:acceptance-result:{action.action_id}",
        )

    def verify_action(self, action, execution):
        self.events.append(("verify", action.action_id))
        return PostconditionResult(
            action.action_id,
            action.postcondition_ref,
            "SATISFIED",
            f"acceptance:evidence:verify:{action.action_id}",
        )

    def compensate_action(self, action, snapshot):
        self.events.append(("compensate", action.action_id))
        return CompensationResult(
            action.action_id,
            snapshot.snapshot_ref,
            "RESTORED",
            f"acceptance:evidence:compensate:{action.action_id}",
        )

    def success_receipt(self, transaction_plan, musical_plan, compilation, snapshot):
        self.events.append(("receipt", transaction_plan.transaction_id))
        self.transaction_id = transaction_plan.transaction_id
        return TransactionReceipt(
            transaction_plan.transaction_id,
            transaction_plan.operation_id,
            snapshot.snapshot_ref,
            f"acceptance:receipt:{transaction_plan.transaction_id}",
            f"acceptance:evidence:success:{transaction_plan.transaction_id}",
            f"sha256:acceptance-transaction:{transaction_plan.transaction_id}",
        )


class RouteProvider:
    def __init__(self, *, runtime: HostRuntimeIdentity, workspace_id: str, driver: RecordingDriver):
        self.runtime = runtime
        self.workspace_id = workspace_id
        self.driver = driver
        self.intents: list[str] = []

    def resolve(self, headquarters, *, song_id: str, artist_intent: str) -> ProduceRoute:
        self.intents.append(artist_intent)
        context = headquarters.focus.capture(
            self.workspace_id,
            song_id=song_id,
            runtime=self.runtime,
            observation_evidence_ref="acceptance:focus:produce",
            dimensions=(
                FocusDimension(
                    "TRACK",
                    "OBSERVED_EXACT",
                    ("track:bass",),
                    "acceptance:focus:track:bass",
                ),
                FocusDimension(
                    "SONG_SECTION",
                    "OBSERVED_EXACT",
                    ("section:chorus",),
                    "acceptance:focus:section:chorus",
                ),
            ),
        )
        return ProduceRoute(
            context=context,
            runtime=self.runtime,
            compiler=AcceptanceCompiler(),
            driver=self.driver,
            required_dimensions=("TRACK", "SONG_SECTION"),
            verification_refs=("verify:track:bass:level",),
            constraints=("Preserve the vocal",),
            locked_element_refs=("track:vocal",),
            editable_element_refs=("track:bass",),
            provenance_refs=("acceptance:reasoning:produce",),
            route_evidence_ref="acceptance:route:produce",
        )


@dataclass(frozen=True)
class Fixture:
    data_root: Path
    state_root: Path
    profile_id: str
    song_id: str
    version_id: str
    workspace_id: str
    runtime: HostRuntimeIdentity


def fixture(tmp_path: Path) -> Fixture:
    data_root = (tmp_path / "data").resolve()
    state_root = (tmp_path / "state").resolve()
    hq = HeadquartersMemory.create(data_root, "Produce Artist")
    try:
        song = hq.store.create_song("Produce Reachability Song")
        version = hq.store.create_version(song.id, label="v1")
        runtime = HostRuntimeIdentity.from_runtime_labels(
            host_family="ABLETON_LIVE",
            version="12.1",
            edition="Suite",
            os_name="Linux",
            machine="x86_64",
        )
        workspace = hq.workspaces.create(
            song.id,
            runtime=runtime,
            location_ref="file:///acceptance/produce-reachability.als",
        )
        return Fixture(
            data_root=data_root,
            state_root=state_root,
            profile_id=hq.store.profile_id,
            song_id=song.id,
            version_id=version.id,
            workspace_id=workspace.id,
            runtime=runtime,
        )
    finally:
        hq.close()


class FormParser(HTMLParser):
    def __init__(self, target_action: str) -> None:
        super().__init__()
        self.target_action = target_action
        self.inside = False
        self.current: dict[str, str] = {}
        self.forms: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        values = dict(attrs)
        if tag == "form" and values.get("action") == self.target_action:
            self.inside = True
            self.current = {}
            return
        if self.inside and tag == "input" and values.get("name") in {"csrf", "action"}:
            self.current[str(values["name"])] = str(values.get("value", ""))

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self.inside:
            self.forms.append(dict(self.current))
            self.current = {}
            self.inside = False


def form_fields(page: str, action: str) -> dict[str, str]:
    parser = FormParser(action)
    parser.feed(page)
    assert len(parser.forms) == 1
    fields = parser.forms[0]
    assert fields.get("csrf")
    assert fields.get("action")
    return fields


def get(shell: ConsumerShell, path: str) -> tuple[int, str]:
    try:
        with urlopen(Request(shell.address.origin + path, method="GET"), timeout=2.0) as response:
            return response.status, response.read().decode("utf-8")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def post(shell: ConsumerShell, path: str, fields: dict[str, str]) -> tuple[int, str]:
    body = urlencode(fields).encode("utf-8")
    request = Request(
        shell.address.origin + path,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Content-Length": str(len(body)),
            "Origin": shell.address.origin,
        },
    )
    try:
        with urlopen(request, timeout=2.0) as response:
            return response.status, response.read().decode("utf-8")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def test_normal_song_surface_reaches_plan_authority_transaction_and_verified_receipt(tmp_path: Path) -> None:
    fx = fixture(tmp_path)
    driver = RecordingDriver()
    provider = RouteProvider(runtime=fx.runtime, workspace_id=fx.workspace_id, driver=driver)
    shell = ConsumerShell(
        data_root=fx.data_root,
        state_root=fx.state_root,
        process=process(),
        probe=Probe(),
    )
    bind_produce_route_provider(shell, provider)
    shell.start()
    try:
        status, song_page = get(shell, "/song")
        assert status == 200
        assert "Produce with N0TE" in song_page
        assert "Host route available for bounded Produce preparation" in song_page

        status, produce_page = get(shell, "/produce")
        assert status == 200
        assert "Describe the musical result, not the plumbing" in produce_page
        preview_fields = form_fields(produce_page, "/produce/preview")
        preview_fields["artist_intent"] = (
            "Make the chorus bass feel more confident without masking the vocal."
        )

        status, preview_page = post(shell, "/produce/preview", preview_fields)
        assert status == 200
        assert "Review this exact change" in preview_page
        assert "Nothing has been changed in the DAW yet." in preview_page
        assert "Ableton Live 12.1" in preview_page
        assert "Track" in preview_page and "track:bass" in preview_page
        assert "Song Section" in preview_page and "section:chorus" in preview_page
        assert "track.level.set" in preview_page
        assert "verify:track:bass:level" in preview_page
        assert driver.events == []
        assert provider.intents == [
            "Make the chorus bass feel more confident without masking the vocal."
        ]

        execute_fields = form_fields(preview_page, "/produce/execute")
        status, result_page = post(shell, "/produce/execute", execute_fields)
        assert status == 200
        assert "Produce execution verified complete." in result_page
        assert [kind for kind, _ in driver.events] == [
            "snapshot",
            "execute",
            "verify",
            "receipt",
        ]
    finally:
        shell.stop()

    assert driver.transaction_id is not None
    reopened = HeadquartersMemory.open(fx.data_root, fx.profile_id)
    try:
        history = reopened.transactions.history(driver.transaction_id)
        assert history.operation.recorded_state == "SUCCEEDED"
        assert history.operation.song_id == fx.song_id
        assert history.operation.version_id == fx.version_id
        assert history.requires_recovery_review is False
        assert [step.step_id for step in history.steps] == ["raise-bass-level"]
    finally:
        reopened.close()


def test_unconnected_host_stays_truthfully_unavailable_without_manual_pretending(tmp_path: Path) -> None:
    fx = fixture(tmp_path)
    shell = ConsumerShell(
        data_root=fx.data_root,
        state_root=fx.state_root,
        process=process(),
        probe=Probe(),
    )
    shell.start()
    try:
        status, page = get(shell, "/produce")
        assert status == 200
        assert "No verified DAW execution route connected" in page
        assert "will not pretend manual guidance or an unverified host feature is executable automation" in page
        assert "Prepare change" not in page
    finally:
        shell.stop()


def test_stale_focus_after_preview_refuses_before_operation_or_driver_mutation(tmp_path: Path) -> None:
    fx = fixture(tmp_path)
    driver = RecordingDriver()
    provider = RouteProvider(runtime=fx.runtime, workspace_id=fx.workspace_id, driver=driver)
    shell = ConsumerShell(
        data_root=fx.data_root,
        state_root=fx.state_root,
        process=process(),
        probe=Probe(),
    )
    bind_produce_route_provider(shell, provider)
    launched = shell.runtime.launch(
        profile_id=fx.profile_id,
        process=process(),
        probe=Probe(),
    )
    assert launched.status == "STARTED"
    try:
        preview = _prepare_preview(
            shell,
            "Raise the chorus bass while preserving the vocal.",
        )
        shell._produce_preview = preview
        execute_token = _preview_token(shell, preview)

        shell.runtime.headquarters.workspaces.reconcile_existing(
            fx.workspace_id,
            song_id=fx.song_id,
            relation="SAME_OR_MOVED",
            runtime=fx.runtime,
            location_ref="file:///acceptance/produce-reachability-moved.als",
        )

        with pytest.raises(FocusUncertainError):
            _post_execute(shell, None, {"action": execute_token})

        assert driver.events == []
        operation_count = shell.runtime.headquarters.store._conn.execute(
            "SELECT COUNT(*) AS count FROM operations"
        ).fetchone()
        transaction_count = shell.runtime.headquarters.store._conn.execute(
            "SELECT COUNT(*) AS count FROM transactions"
        ).fetchone()
        assert int(operation_count["count"]) == 0
        assert int(transaction_count["count"]) == 0
    finally:
        assert shell.runtime.quit().status == "STOPPED"
