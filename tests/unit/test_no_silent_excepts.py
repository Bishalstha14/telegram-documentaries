"""The silent-failure guard: no `except: pass` anywhere in `src/` (TG5).

A bare `except:` swallows a class of failures; an `except` whose body is only
`pass` or `...` swallows them silently. Either shape makes a bug impossible
to see in the logs. This module walks the real source tree and proves neither
shape exists - and, first, proves the checker itself can be made to fail, so
the guard is a guard and not a formality.
"""

import ast
import textwrap
from pathlib import Path

#: The tree the guard defends. `resolve()` so the path is stable no matter
#: how pytest invokes the module.
SRC = Path(__file__).resolve().parents[2] / "src"


def _is_silent(handler: ast.ExceptHandler) -> bool:
    """True when `handler` swallows its failure instead of reacting (TG5).

    Either the `except` names nothing - `except:` takes whatever comes - or
    its whole body is `pass` / `...`, which runs without saying anything.
    Anything else - a log, a raise, a return - is a reaction, not a swallow.
    """
    if handler.type is None:
        return True
    if len(handler.body) != 1:
        return False
    statement = handler.body[0]
    if isinstance(statement, ast.Pass):
        return True
    if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant):
        return statement.value.value is Ellipsis
    return False


def find_silent_handlers(root: Path) -> list[tuple[str, int, str]]:
    """Every (file, line, shape) in `root` whose handler swallows failures.

    Shape is a short description used in the failure message: `bare except:`
    or `except X: pass` / `except X: ...`, always naming the victim of the
    `except` too - never its body.
    """
    offenders: list[tuple[str, int, str]] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            if not _is_silent(node):
                continue
            if node.type is None:
                shape = "bare except:"
            elif isinstance(node.body[0], ast.Pass):
                shape = f"except {ast.unparse(node.type)}: pass"
            else:
                shape = f"except {ast.unparse(node.type)}: ..."
            offenders.append((str(path), node.lineno, shape))
    return offenders


def _describe(offenders: list[tuple[str, int, str]]) -> str:
    """One line per offender, naming file and line so the fix is a hop away."""
    return "\n".join(f"{path}:{line}: {shape}" for path, line, shape in offenders)


def test_the_checker_detects_every_offender_shape() -> None:
    """The guard can fail: each swallow shape is found, none escapes.

    Run against an in-memory source - a guard that cannot be made to fail is
    not a guard, and this is the proof it fires.
    """
    source = textwrap.dedent(
        """\
        def f() -> None:
            try:
                boom()
            except RuntimeError:
                pass

        def g() -> None:
            try:
                boom()
            except:  # noqa: E722, A001 - deliberate; the self-test must catch it
                print("caught")

        def h() -> None:
            try:
                boom()
            except ValueError:
                ...

        def fine() -> None:
            try:
                boom()
            except KeyError:
                logger.warning("boom")

        def fine_too() -> None:
            try:
                boom()
            except (KeyError, TypeError) as exc:
                raise SomethingElse("wrapped") from exc

        def fine_three() -> None:
            try:
                boom()
            except (IndexError, LookupError):
                logger.warning("legacy index")
            return
        """
    )
    offenders = find_silent_handlers_tree(ast.parse(source))

    assert len(offenders) == 3, offenders
    shapes = sorted(shape for _, _, shape in offenders)
    assert shapes == [
        "bare except:",
        "except RuntimeError: pass",
        "except ValueError: ...",
    ]


def test_no_silent_except_handlers_anywhere_in_src() -> None:
    """The real tree: no swallow, anywhere, ever committed (TG5)."""
    offenders = find_silent_handlers(SRC)

    assert not offenders, (
        "silent except handlers found in src/ - every except must log or "
        "raise, never swallow:\n" + _describe(offenders)
    )


def find_silent_handlers_tree(tree: ast.Module) -> list[tuple[str, int, str]]:
    """Same scan as `find_silent_handlers`, over an already-parsed tree.

    The in-memory self-test cannot run `rglob`, so the scan core lives here
    and the file walk builds on it.
    """
    offenders: list[tuple[str, int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if not _is_silent(node):
            continue
        if node.type is None:
            shape = "bare except:"
        elif isinstance(node.body[0], ast.Pass):
            shape = f"except {ast.unparse(node.type)}: pass"
        else:
            shape = f"except {ast.unparse(node.type)}: ..."
        offenders.append(("<memory>", node.lineno, shape))
    return offenders
