"""Tests des parseurs et des chemins principaux des commandes Telegram."""

from __future__ import annotations

import ast
import re
from types import SimpleNamespace
from pathlib import Path

import pytest

import bot


def test_all_registered_commands_are_implemented_and_documented():
    source_path = Path(bot.__file__).resolve()
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    async_functions = {
        node.name for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef)
    }
    registered: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_handler"
            and node.args
        ):
            continue
        handler = node.args[0]
        if (
            isinstance(handler, ast.Call)
            and isinstance(handler.func, ast.Name)
            and handler.func.id == "CommandHandler"
            and len(handler.args) >= 2
            and isinstance(handler.args[0], ast.Constant)
            and isinstance(handler.args[1], ast.Name)
        ):
            registered.append((str(handler.args[0].value), handler.args[1].id))

    help_start = source.index("async def cmd_help")
    help_end = source.index("# ── Callback router")
    help_text = source[help_start:help_end]
    documented = set(re.findall(r"<code>/([a-z0-9]+)", help_text))

    assert registered
    assert all(function in async_functions for _, function in registered)
    assert {command for command, _ in registered} <= documented


def test_command_parsers_accept_expected_formats():
    assert bot._parse_teams(["Paris", "vs", "Lyon"]) == ("Paris", "Lyon")
    assert bot._parse_teams(["Paris", "-", "Lyon"]) == ("Paris", "Lyon")
    assert bot._parse_teams(["Paris"]) is None
    assert bot._parse_score("2-1") == (2, 1)
    assert bot._parse_score("2:0") == (2, 0)
    assert bot._parse_score("-1") is None
    assert bot._parse_score("x-y") is None


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Étape 1/3 — 12 matchs", {"step": 1, "total": 12}),
        ("Étape 2/3 — validation : 5/24", {"step": 2, "processed": 5, "total": 24}),
        ("Terminé — 24 matchs", {"step": 3, "done": True, "processed": 24}),
        # Étape 3 avec compteur X/Y et noms d'équipes défilants
        (
            "Étape 3/3 — 12/24 · PSG – Lyon (+8 stats étendues)",
            {"step": 3, "done": False, "processed": 12, "total": 24,
             "home": "PSG", "away": "Lyon"},
        ),
        # Étape 3 sans équipes (premier message de l'étape)
        ("Étape 3/3 — 1/24 · Arsenal – Chelsea (+5 stats étendues)",
         {"step": 3, "processed": 1, "total": 24}),
    ],
)
def test_scan_progress_label_parser(label, expected):
    parsed = bot._parse_scan_label(label)
    for key, value in expected.items():
        assert parsed[key] == value


class FakeMessage:
    def __init__(self):
        self.replies = []
        self.edits = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))
        return self

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


class FakeContext:
    def __init__(self, args=None):
        self.args = args or []
        self.user_data = {}