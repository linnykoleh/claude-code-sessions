from ccs.launch import resume_command
from ccs.sessions import Live

SID = "11111111-2222-3333-4444-555555555555"


def never_ask(question):
    raise AssertionError(f"asked: {question}")


def test_a_closed_session_is_resumed():
    assert resume_command(SID, None, ask=never_ask) == ["claude", "--resume", SID]


def test_a_background_session_is_attached():
    live = Live(pid=1, status="busy", job_id="c0ffee12")
    assert resume_command(SID, live, ask=never_ask) == ["claude", "attach", "c0ffee12"]


def test_a_session_open_elsewhere_asks_first():
    live = Live(pid=4242, status="idle")
    questions = []

    def answer(reply):
        def ask(question):
            questions.append(question)
            return reply
        return ask

    assert resume_command(SID, live, ask=answer("c")) == ["claude", "--resume", SID, "--fork-session"]
    assert resume_command(SID, live, ask=answer("r")) == ["claude", "--resume", SID]
    assert resume_command(SID, live, ask=answer("")) is None
    assert resume_command(SID, live, ask=answer("x")) is None
    assert "already open" in questions[0] and "4242" in questions[0]

