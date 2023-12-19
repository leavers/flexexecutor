import os

import nox
from nox import Session
from nox.command import CommandFailed


@nox.session(venv_backend="none")
def shell_completion(session: Session):
    session.run("echo", 'eval "$(register-python-argcomplete nox)"', silent=True)


@nox.session(venv_backend="none")
def clean(session: Session):
    session.run(
        "rm",
        "-rf",
        ".mypy_cache",
        ".pytype",
        ".pytest_cache",
        ".pytype_output",
        "build",
        "dist",
        "html_cov",
        "html_doc",
        "logs",
    )
    session.run(
        "sh",
        "-c",
        "find . | grep -E '(__pycache__|\.pyc|\.pyo$$)' | xargs rm -rf",
    )


@nox.session(python="3.8")
def format(session: Session):
    try:
        session.run("taplo", "fmt", "pyproject.toml")
    except CommandFailed:
        session.warn(
            "Seems that `taplo` is not found, skip formatting `pyproject.toml`. "
            "(Refer to https://taplo.tamasfe.dev/ for information on how to install "
            "`taplo`)"
        )
    session.run("autoflake", "--version")
    session.run(
        "autoflake",
        "flexexecutor",
        "tests",
        "noxfile.py",
    )
    session.run("isort", "--vn")
    session.run(
        "isort",
        "flexexecutor",
        "tests",
        "noxfile.py",
    )
    session.run("ruff", "--version")
    session.run(
        "ruff",
        "format",
        "flexexecutor",
        "tests",
        "noxfile.py",
    )


@nox.session(python="3.8")
def format_check(session: Session):
    try:
        session.run("taplo", "check", "pyproject.toml")
    except CommandFailed:
        session.warn(
            "Seems that `taplo` is not found, skip checking `pyproject.toml`. "
            "(Refer to https://taplo.tamasfe.dev/ for information on how to install "
            "`taplo`)"
        )
    session.run("autoflake", "--version")
    session.run(
        "autoflake",
        "--check-diff",
        "flexexecutor",
        "tests",
        "noxfile.py",
    )
    session.run("isort", "--vn")
    session.run(
        "isort",
        "--check",
        "--diff",
        "flexexecutor",
        "tests",
        "noxfile.py",
    )
    session.run("ruff", "--version")
    session.run(
        "ruff",
        "format",
        "--check",
        "--diff",
        "flexexecutor",
        "tests",
        "noxfile.py",
    )


@nox.session(python="3.8")
def mypy(session: Session):
    session.run("mypy", "--version")
    session.log(
        "If you encountered "
        "\"AttributeError: attribute 'TypeInfo' of '_fullname' undefined\", "
        "please try to execute `rm -rf .mypy_cache`"
    )
    session.run("mypy", "flexexecutor.py", "noxfile.py")


@nox.session(
    # use either `python=False` or `venv_backend="none"` to disable virtualenv
    venv_backend="none",
)
def test_simple(session: Session):
    session.run(
        "pytest",
        "--cov-config",
        "pyproject.toml",
        "tests",
    )


@nox.session(python=["3.6", "3.8", "3.10", "3.11", "3.12"])
def test_all(session: Session):
    session.install("pytest")
    session.run("pytest", "tests/")
