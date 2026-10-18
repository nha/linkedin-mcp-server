"""LOGIN_COMMAND: a server nobody watches never signs in by itself.

Started once per coding-agent session, the server used to open a login window
on the user's screen whenever the session had expired, from a call nobody was
there to answer. With ``LOGIN_COMMAND`` set every tool fails at once instead,
naming the command, and no browser, import or login is started.
"""

import asyncio
import time

import pytest
from fastmcp import Client

COMMAND = "make -C /repo linkedin-login"

#: Tools that never touch LinkedIn, so they have nothing to refuse.
_NO_BROWSER = {"close_session"}


def _args_for(tool) -> dict:
    """Placeholder values for a tool's required arguments."""
    by_type = {"string": "x", "integer": 1, "number": 1, "boolean": False}
    props = tool.inputSchema.get("properties", {})
    args = {}
    for name in tool.inputSchema.get("required", []):
        schema = props[name]
        kind = schema.get("type") or next(
            (s.get("type") for s in schema.get("anyOf", []) if s.get("type") != "null"),
            "string",
        )
        args[name] = [] if kind == "array" else by_type.get(kind, "x")
    return args


@pytest.fixture
def no_login(isolate_profile_dir, monkeypatch):
    """A server with LOGIN_COMMAND, an empty profile, and a display available.

    The display check is stubbed to the macOS answer, where a headed window can
    exist, so nothing here passes merely because no window could open.
    """
    import linkedin_mcp_server.bootstrap as bootstrap
    import linkedin_mcp_server.core.browser as core_browser
    import linkedin_mcp_server.dependencies as dependencies
    import linkedin_mcp_server.setup as setup

    monkeypatch.setenv("LOGIN_COMMAND", COMMAND)
    monkeypatch.setenv("HEADLESS", "false")
    monkeypatch.setattr(core_browser, "hidden_target_is_supported", lambda: True)

    started: list[str] = []

    def record(name):
        async def refuse(*args, **kwargs):
            started.append(name)
            raise AssertionError(f"{name} must not run with LOGIN_COMMAND set")

        return refuse

    monkeypatch.setattr(setup, "interactive_login", record("interactive_login"))
    monkeypatch.setattr(bootstrap, "interactive_login", record("interactive_login"))
    monkeypatch.setattr(bootstrap, "_run_login_flow", record("login_flow"))
    monkeypatch.setattr(bootstrap, "_try_auto_import_session", record("auto_import"))
    monkeypatch.setattr(dependencies, "get_or_create_browser", record("browser_launch"))
    # As if the command had just run: these tests never execute it. The ones
    # about running it set this back to None.
    monkeypatch.setattr(bootstrap, "_login_run_at", time.monotonic())
    monkeypatch.setattr(bootstrap, "_login_run_detail", None)
    return started


async def test_every_tool_fails_at_once_naming_the_command(no_login):
    from linkedin_mcp_server.config import get_config
    from linkedin_mcp_server.server import create_mcp_server

    async with Client(create_mcp_server()) as client:
        tools = [t for t in await client.list_tools() if t.name not in _NO_BROWSER]
        assert len(tools) > 10
        loop = asyncio.get_running_loop()
        for tool in tools:
            began = loop.time()
            result = await client.call_tool(
                tool.name, _args_for(tool), raise_on_error=False
            )
            text = result.content[0].text
            assert result.is_error, tool.name
            assert "session is dead" in text and COMMAND in text, (tool.name, text)
            assert loop.time() - began < 2, tool.name

    config = get_config()
    assert config.browser.headless is True  # over HEADLESS=false
    assert config.browser.auto_import_from_browser is False
    assert no_login == []


async def test_concurrent_calls_get_the_same_answer_not_busy(no_login):
    from linkedin_mcp_server.server import create_mcp_server

    async with Client(create_mcp_server()) as client:
        results = await asyncio.gather(
            *(client.call_tool("get_inbox", {}, raise_on_error=False) for _ in range(5))
        )
    texts = {r.content[0].text for r in results}
    assert len(texts) == 1, texts
    (text,) = texts
    assert COMMAND in text and "currently using the browser" not in text
    assert no_login == []


async def test_a_dead_session_is_retired_and_no_login_starts(no_login):
    """The expired case: files on disk, but LinkedIn rejected the cookies."""
    import linkedin_mcp_server.bootstrap as bootstrap
    from linkedin_mcp_server.config import get_config
    from linkedin_mcp_server.core.exceptions import AuthenticationError
    from linkedin_mcp_server.dependencies import handle_auth_error
    from linkedin_mcp_server.drivers.browser import get_profile_dir
    from linkedin_mcp_server.exceptions import LoginRequiredError
    from linkedin_mcp_server.session_state import (
        portable_cookie_path,
        write_source_state,
    )

    get_config()
    profile = get_profile_dir()
    (profile / "Default").mkdir(parents=True, exist_ok=True)
    (profile / "Default" / "Cookies").write_text("placeholder")
    portable_cookie_path(profile).write_text("[]")
    write_source_state(profile)
    assert bootstrap.login_required_error() is None

    with pytest.raises(LoginRequiredError, match="linkedin-login"):
        await handle_auth_error(AuthenticationError("redirect loop"), None)

    # Retired, so every later call answers from disk without a browser.
    assert isinstance(bootstrap.login_required_error(), LoginRequiredError)
    assert bootstrap.get_bootstrap_state().login_task is None
    assert no_login == []


def _write_session(profile) -> None:
    from linkedin_mcp_server.session_state import (
        portable_cookie_path,
        write_source_state,
    )

    (profile / "Default").mkdir(parents=True, exist_ok=True)
    (profile / "Default" / "Cookies").write_text("placeholder")
    portable_cookie_path(profile).write_text("[]")
    write_source_state(profile)


async def test_close_session_still_works_without_a_session(no_login):
    from linkedin_mcp_server.server import create_mcp_server

    async with Client(create_mcp_server()) as client:
        result = await client.call_tool("close_session", {}, raise_on_error=False)
    assert not result.is_error, result.content[0].text


async def test_a_dead_session_that_cannot_be_retired_is_still_refused(
    no_login, monkeypatch
):
    """Retiring can fail (a held lease, a permission). The files stay, so the
    rejected generation is remembered rather than tried again in a browser."""
    import linkedin_mcp_server.bootstrap as bootstrap
    from linkedin_mcp_server.config import get_config
    from linkedin_mcp_server.drivers.browser import get_profile_dir
    from linkedin_mcp_server.exceptions import (
        AuthenticationBootstrapFailedError,
        LoginRequiredError,
    )

    get_config()
    _write_session(get_profile_dir())

    def cannot(*args, **kwargs):
        raise AuthenticationBootstrapFailedError("profile in use")

    monkeypatch.setattr(bootstrap, "_force_move_auth_state_aside", cannot)
    with pytest.raises(LoginRequiredError):
        await bootstrap.invalidate_auth_and_trigger_relogin(
            stale_generation=bootstrap.current_login_generation()
        )
    assert bootstrap._auth_ready()
    assert isinstance(bootstrap.login_required_error(), LoginRequiredError)

    # A fresh sign-in writes a new generation, which lifts it.
    _write_session(get_profile_dir())
    assert bootstrap.login_required_error() is None
    assert no_login == []


async def test_a_session_that_appeared_meanwhile_is_used(no_login):
    import linkedin_mcp_server.bootstrap as bootstrap
    from linkedin_mcp_server.config import get_config
    from linkedin_mcp_server.drivers.browser import get_profile_dir

    get_config()
    _write_session(get_profile_dir())
    await bootstrap.start_login_if_needed()  # returns, does not raise
    assert no_login == []


def test_without_login_command_nothing_changes(monkeypatch):
    from linkedin_mcp_server.config import get_config

    monkeypatch.setenv("HEADLESS", "false")
    config = get_config()
    assert config.browser.login_command is None
    assert config.browser.headless is False


async def test_a_dead_session_leaves_the_profile_free_for_other_servers(
    no_login, monkeypatch
):
    """One server per agent session shares the profile by taking turns on its
    lease. A login used to hold that lease for up to its 30-minute timeout, so
    one session's pending sign-in blocked every other session's sends. Now the
    expired session fails every concurrent call and the profile is free again
    at once, for another process as much as this one."""
    import subprocess
    import sys

    import linkedin_mcp_server.bootstrap as bootstrap
    import linkedin_mcp_server.dependencies as dependencies
    from linkedin_mcp_server.config import get_config
    from linkedin_mcp_server.core.exceptions import AuthenticationError
    from linkedin_mcp_server.drivers.browser import get_profile_dir
    from linkedin_mcp_server.server import create_mcp_server

    get_config()
    _write_session(get_profile_dir())

    async def a_browser():
        return object()

    async def rejected():
        raise AuthenticationError("ERR_TOO_MANY_REDIRECTS")

    monkeypatch.setattr(dependencies, "get_or_create_browser", a_browser)
    monkeypatch.setattr(dependencies, "ensure_authenticated", rejected)
    # The managed browser counts as installed, so the call reaches LinkedIn.
    monkeypatch.setattr(bootstrap, "_uses_custom_chrome", lambda: True)

    async with Client(create_mcp_server()) as client:
        results = await asyncio.gather(
            *(
                client.call_tool("send_message", _args_for_send(), raise_on_error=False)
                for _ in range(4)
            )
        )
    texts = {r.content[0].text for r in results}
    assert len(texts) == 1 and COMMAND in texts.pop()

    other_server = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from pathlib import Path; "
            "from linkedin_mcp_server.profile_lease import get_profile_lease; "
            "print(get_profile_lease(Path(sys.argv[1])).try_acquire())",
            str(get_profile_dir()),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert other_server.stdout.strip() == "True", other_server.stderr
    assert no_login == []


def _args_for_send() -> dict:
    return {"linkedin_username": "someone", "message": "hello", "confirm_send": True}


def _command(tmp_path, code: str) -> str:
    """A LOGIN_COMMAND that appends a line to runs.txt, then runs *code*."""
    import shlex
    import sys

    runs = tmp_path / "runs.txt"
    body = f"open({str(runs)!r}, 'a').write('x\\n'); {code}"
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(body)}"


async def test_the_command_runs_and_a_signed_in_call_carries_on(
    no_login, tmp_path, monkeypatch
):
    """The bridge case: the command signs in, so the call goes on, with no error."""
    import linkedin_mcp_server.bootstrap as bootstrap
    from linkedin_mcp_server.config import get_config

    session = tmp_path / "session"
    monkeypatch.setenv(
        "LOGIN_COMMAND", _command(tmp_path, f"open({str(session)!r}, 'w')")
    )
    get_config()
    monkeypatch.setattr(bootstrap, "_login_run_at", None)
    monkeypatch.setattr(bootstrap, "_auth_ready", lambda *a, **k: session.exists())

    assert await bootstrap.sign_in_or_error() is None
    assert await bootstrap.sign_in_or_error() is None
    assert (tmp_path / "runs.txt").read_text() == "x\n"
    assert no_login == []


async def test_a_failing_command_is_named_with_its_output_and_not_rerun(
    no_login, tmp_path, monkeypatch
):
    import linkedin_mcp_server.bootstrap as bootstrap
    from linkedin_mcp_server.config import get_config
    from linkedin_mcp_server.exceptions import LoginRequiredError

    command = _command(
        tmp_path, "print('make -C /repo linkedin-login, by hand'); raise SystemExit(3)"
    )
    monkeypatch.setenv("LOGIN_COMMAND", command)
    get_config()
    monkeypatch.setattr(bootstrap, "_login_run_at", None)

    first = await bootstrap.sign_in_or_error()
    again = await bootstrap.sign_in_or_error()
    assert isinstance(first, LoginRequiredError)
    assert "exit 3: make -C /repo linkedin-login, by hand" in str(first)
    assert str(again) == str(first)
    assert (tmp_path / "runs.txt").read_text() == "x\n"
    assert no_login == []
