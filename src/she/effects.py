"""Visible file and environment effects, read straight from the syntax and a few per-command rules."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from she.knowledge import QUIET_TARGETS
from she.models import EnvEffect, FileEffect
from she.parser import ParsedCommand, Redirect

DECLARATION_BUILTINS = frozenset({"export", "declare", "typeset", "local", "readonly"})
DIRECTORY_CHANGERS = frozenset({"cd", "pushd"})
QUIET_PREFIXES = ("/dev/fd/", "/dev/tty")


@dataclass(frozen=True, slots=True)
class Invocation:
    """A command after wrapper unwrapping: what actually runs, and env set by `env A=1` style prefixes."""

    name: str
    args: Tuple[str, ...] = ()
    env: Tuple[Tuple[str, Optional[str]], ...] = field(default_factory=tuple)


def _visible_file(path: str, op: str, detail: str) -> FileEffect:
    return FileEffect(path=path, op=op, basis="visible", detail=detail)  # type: ignore[arg-type]


def _visible_env(name: str, op: str, value: Optional[str], detail: str) -> EnvEffect:
    return EnvEffect(name=name, op=op, basis="visible", value=value, detail=detail)  # type: ignore[arg-type]


def is_quiet_target(path: str) -> bool:
    return path in QUIET_TARGETS or path.startswith(QUIET_PREFIXES)


def positionals(args: Sequence[str], value_options: frozenset[str] = frozenset()) -> List[str]:
    """Arguments that aren't options; options listed in `value_options` also swallow the next word."""
    result: List[str] = []
    skip_next = False
    options_done = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if options_done or arg == "-" or not arg.startswith("-"):
            result.append(arg)
        elif arg == "--":
            options_done = True
        elif arg in value_options:
            skip_next = True
    return result


def option_value(args: Sequence[str], names: Iterable[str]) -> List[str]:
    """Values of options like `-o FILE`, `-oFILE`, `--output FILE` and `--output=FILE`."""
    wanted = tuple(names)
    values: List[str] = []
    for index, arg in enumerate(args):
        if arg == "--":
            break
        for name in wanted:
            if arg == name and index + 1 < len(args):
                values.append(args[index + 1])
            elif name.startswith("--") and arg.startswith(name + "="):
                values.append(arg[len(name) + 1 :])
            elif not name.startswith("--") and arg.startswith(name) and len(arg) > len(name) and arg[1] != "-":
                values.append(arg[len(name) :])
    return values


def has_flag(args: Sequence[str], short: str, long: Optional[str] = None) -> bool:
    """True if a short flag letter appears (alone or combined, e.g. `-rf`) or the long form is given."""
    for arg in args:
        if arg == "--":
            return False
        if long is not None and arg == long:
            return True
        if arg.startswith("-") and not arg.startswith("--") and short in arg[1:]:
            return True
    return False


# -- per-command rules ------------------------------------------------------------------------

def _delete(name: str, args: Sequence[str]) -> List[FileEffect]:
    return [_visible_file(path, "delete", f"from {name}") for path in positionals(args)]


def _create(name: str, args: Sequence[str]) -> List[FileEffect]:
    value_options = frozenset({"-m", "-t", "-r", "-d", "-A"})
    return [_visible_file(path, "create", f"from {name}") for path in positionals(args, value_options)]


def _link(name: str, args: Sequence[str]) -> List[FileEffect]:
    paths = positionals(args)
    return [_visible_file(paths[-1], "create", f"from {name}")] if len(paths) >= 2 else []


def _read(name: str, args: Sequence[str]) -> List[FileEffect]:
    value_options = frozenset({"-n", "-c", "-b", "-t", "-k", "-o"})
    return [_visible_file(path, "read", f"from {name}") for path in positionals(args, value_options) if path != "-"]


def _source(name: str, args: Sequence[str]) -> List[FileEffect]:
    paths = positionals(args)
    return [_visible_file(paths[0], "read", f"from {name}")] if paths else []


def _copy(name: str, args: Sequence[str]) -> List[FileEffect]:
    paths = positionals(args, frozenset({"-t", "-S"}))
    if len(paths) < 2:
        return []
    *sources, destination = paths
    return [*(_visible_file(path, "read", f"from {name}") for path in sources), _visible_file(destination, "write", f"from {name}")]


def _move(name: str, args: Sequence[str]) -> List[FileEffect]:
    paths = positionals(args, frozenset({"-t", "-S"}))
    if len(paths) < 2:
        return []
    *sources, destination = paths
    return [*(_visible_file(path, "delete", f"from {name}") for path in sources), _visible_file(destination, "write", f"from {name}")]


def _chmod(name: str, args: Sequence[str]) -> List[FileEffect]:
    paths = positionals(args)
    return [_visible_file(path, "chmod", f"from {name}") for path in paths[1:]]


def _tee(name: str, args: Sequence[str]) -> List[FileEffect]:
    op = "append" if has_flag(args, "a", "--append") else "write"
    return [_visible_file(path, op, f"from {name}") for path in positionals(args)]


def _sed(name: str, args: Sequence[str]) -> List[FileEffect]:
    in_place = False
    has_script_option = False
    script_files: List[str] = []
    remaining: List[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        match arg:
            case "-i" | "--in-place":
                in_place = True
                if index + 1 < len(args) and args[index + 1] == "":
                    index += 1
            case _ if arg.startswith("-i") or arg.startswith("--in-place="):
                in_place = True
            case "-e" | "--expression":
                has_script_option = True
                index += 1
            case "-f" | "--file":
                has_script_option = True
                if index + 1 < len(args):
                    script_files.append(args[index + 1])
                index += 1
            case _ if arg.startswith("-") and arg != "-":
                in_place = in_place or ("i" in arg[1:] and not arg.startswith("--"))
            case _:
                remaining.append(arg)
        index += 1
    files = remaining if has_script_option else remaining[1:]
    op = "write" if in_place else "read"
    return [
        *(_visible_file(path, "read", "sed script (-f)") for path in script_files),
        *(_visible_file(path, op, "from sed -i" if in_place else "from sed") for path in files),
    ]


def _grep(name: str, args: Sequence[str]) -> List[FileEffect]:
    value_options = frozenset({"-e", "-f", "-m", "-A", "-B", "-C", "-d", "-D", "--regexp", "--file", "--max-count", "-g", "-t", "-T"})
    has_pattern_option = any(
        arg in {"-e", "-f", "--regexp", "--file"} or arg.startswith(("--regexp=", "--file=")) for arg in args
    )
    paths = positionals(args, value_options)
    files = paths if has_pattern_option else paths[1:]
    pattern_files = option_value(args, ("-f", "--file"))
    return [
        *(_visible_file(path, "read", f"{name} patterns (-f)") for path in pattern_files),
        *(_visible_file(path, "read", f"from {name}") for path in files),
    ]


def _find(name: str, args: Sequence[str]) -> List[FileEffect]:
    starts: List[str] = []
    for arg in args:
        if arg.startswith("-") or arg in {"(", "!", ")"}:
            break
        starts.append(arg)
    roots = starts or ["."]
    effects = [_visible_file(root, "read", "find start directory") for root in roots]
    if "-delete" in args:
        names = option_value(args, ("-name", "-iname"))
        pattern = names[0] if names else None
        effects.extend(
            _visible_file(f"{root.rstrip('/') or '/'}/**/{pattern}" if pattern else f"{root.rstrip('/') or '/'}/**", "delete", "from -delete")
            for root in roots
        )
    return effects


def _curl(name: str, args: Sequence[str]) -> List[FileEffect]:
    written = [_visible_file(path, "write", f"from {name} -o") for path in option_value(args, ("-o", "--output"))]
    uploaded = [_visible_file(path, "read", f"from {name} -T") for path in option_value(args, ("-T", "--upload-file"))]
    return [*written, *uploaded]


def _wget(name: str, args: Sequence[str]) -> List[FileEffect]:
    documents = [_visible_file(path, "write", f"from {name} -O") for path in option_value(args, ("-O", "--output-document"))]
    logs = [_visible_file(path, "write", f"{name} log (-o)") for path in option_value(args, ("-o", "--output-file"))]
    return [*documents, *logs]


def _ssh(name: str, args: Sequence[str]) -> List[FileEffect]:
    identities = [_visible_file(path, "read", f"{name} identity (-i)") for path in option_value(args, ("-i",))]
    configs = [_visible_file(path, "read", f"{name} config (-F)") for path in option_value(args, ("-F",))]
    return [*identities, *configs]


def _dd(name: str, args: Sequence[str]) -> List[FileEffect]:
    reads = [_visible_file(arg[3:], "read", "dd if=") for arg in args if arg.startswith("if=")]
    writes = [_visible_file(arg[3:], "write", "dd of=") for arg in args if arg.startswith("of=")]
    return [*reads, *writes]


Rule = Callable[[str, Sequence[str]], List[FileEffect]]
FILE_RULES: Mapping[str, Rule] = {
    "rm": _delete,
    "unlink": _delete,
    "rmdir": _delete,
    "mkdir": _create,
    "touch": _create,
    "ln": _link,
    "cat": _read,
    "head": _read,
    "tail": _read,
    "less": _read,
    "more": _read,
    "wc": _read,
    "source": _source,
    ".": _source,
    "cp": _copy,
    "mv": _move,
    "chmod": _chmod,
    "chown": _chmod,
    "chgrp": _chmod,
    "tee": _tee,
    "sed": _sed,
    "gsed": _sed,
    "grep": _grep,
    "egrep": _grep,
    "fgrep": _grep,
    "rg": _grep,
    "find": _find,
    "curl": _curl,
    "wget": _wget,
    "ssh": _ssh,
    "scp": _ssh,
    "dd": _dd,
}


def command_file_effects(invocation: Invocation) -> List[FileEffect]:
    """File effects implied by one command's name and arguments."""
    rule = FILE_RULES.get(invocation.name)
    return rule(invocation.name, invocation.args) if rule is not None else []


def redirect_effects(redirects: Iterable[Redirect]) -> List[FileEffect]:
    """File effects of redirects; descriptor duplication (`2>&1`) and /dev/null-style targets are skipped."""
    effects: List[FileEffect] = []
    for redirect in redirects:
        target = redirect.target.text if redirect.target is not None else None
        if target is None or is_quiet_target(target):
            continue
        match redirect.operator:
            case ">" | ">|" | "&>":
                effects.append(_visible_file(target, "write", f"from {redirect.operator} redirect"))
            case ">>" | "&>>":
                effects.append(_visible_file(target, "append", f"from {redirect.operator} redirect"))
            case "<" | "<>":
                effects.append(_visible_file(target, "read", f"from {redirect.operator} redirect"))
            case ">&" if not (target.isdigit() or target == "-"):
                effects.append(_visible_file(target, "write", "from >& redirect"))
            case _:
                continue
    return effects


def env_effects(parsed: ParsedCommand, invocations: Sequence[Invocation]) -> List[EnvEffect]:
    """Environment effects: assignments, export/declare, unset, `$X` reads and directory changes."""
    effects: List[EnvEffect] = []
    effects.extend(_visible_env(item.name, "set", item.value, "assignment") for item in parsed.shell_assignments)
    for command in parsed.commands:
        effects.extend(
            _visible_env(item.name, "set", item.value, "for this command only") for item in command.assignments
            if command.kind == "command"
        )
        match command.kind:
            case "declaration":
                effects.extend(_visible_env(item.name, "set", item.value, command.name.text) for item in command.assignments)
                effects.extend(
                    _visible_env(word.text, "set", None, command.name.text)
                    for word in command.args
                    if not word.text.startswith("-") and word.text.isidentifier()
                )
            case "unset":
                effects.extend(
                    _visible_env(word.text, "unset", None, "unset") for word in command.args if not word.text.startswith("-")
                )
            case _:
                pass
    for invocation in invocations:
        effects.extend(_visible_env(name, "set", value, "env prefix") for name, value in invocation.env)
        match invocation.name:
            case name if name in DIRECTORY_CHANGERS:
                targets = positionals(invocation.args)
                effects.append(_visible_env("PWD", "set", targets[0] if targets else "~", name))
            case "popd":
                effects.append(_visible_env("PWD", "set", None, "popd"))
            case "read":
                effects.extend(
                    _visible_env(name, "set", None, "read")
                    for name in positionals(invocation.args, frozenset({"-p", "-d", "-n", "-N", "-t", "-u", "-a"}))
                    if name.isidentifier()
                )
            case _:
                pass
    local_names = frozenset(parsed.loop_variables)
    effects.extend(
        _visible_env(token.text, "read", None, "expansion") for token in parsed.env_reads if token.text not in local_names
    )
    return dedupe_env(effects)


def dedupe_files(effects: Iterable[FileEffect]) -> List[FileEffect]:
    seen: Dict[Tuple[str, str], FileEffect] = {}
    for effect in effects:
        seen.setdefault((effect.path, effect.op), effect)
    return list(seen.values())


def dedupe_env(effects: Iterable[EnvEffect]) -> List[EnvEffect]:
    seen: Dict[Tuple[str, str], EnvEffect] = {}
    for effect in effects:
        seen.setdefault((effect.name, effect.op), effect)
    return list(seen.values())


def collect(parsed: ParsedCommand, invocations: Sequence[Invocation]) -> Tuple[Tuple[FileEffect, ...], Tuple[EnvEffect, ...]]:
    """All visible effects of a parsed command, given its unwrapped invocations."""
    files: List[FileEffect] = []
    for command in parsed.commands:
        files.extend(redirect_effects(command.redirects))
    for invocation in invocations:
        files.extend(command_file_effects(invocation))
    return tuple(dedupe_files(files)), tuple(dedupe_env(env_effects(parsed, invocations)))
