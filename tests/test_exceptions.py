from linkedin_mcp_server.exceptions import (
    CredentialsNotFoundError,
    LinkedInMCPError,
    SessionExpiredError,
)


def test_base_exception():
    err = LinkedInMCPError("test")
    assert str(err) == "test"


def test_session_expired_default_message():
    err = SessionExpiredError()
    assert "expired" in str(err).lower()


def test_session_expired_custom_message():
    err = SessionExpiredError("custom")
    assert str(err) == "custom"


def test_inheritance():
    assert issubclass(SessionExpiredError, LinkedInMCPError)
    assert issubclass(CredentialsNotFoundError, LinkedInMCPError)


def test_login_required_after_a_run_quotes_its_lines_not_a_rerun():
    from linkedin_mcp_server.exceptions import LoginRequiredError

    ran = (
        "exit 1: linkedin session is dead: to sign in, at the Mac: make linkedin-login"
    )
    after = str(LoginRequiredError("bin/logins dead linkedin", ran))
    assert "Run « bin/logins dead linkedin »" not in after
    assert ran in after
    assert after.endswith(f"retry this tool once signed in. Its output: {ran}")
    before = str(LoginRequiredError("bin/linkedin-login"))
    assert "Run « bin/linkedin-login » to sign in again" in before
