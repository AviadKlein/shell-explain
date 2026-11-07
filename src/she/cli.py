"""`she` command-line entry point (contract §2). LLM/HTTP code is imported lazily."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, List, Optional, Sequence, TextIO, Tuple

from she import __version__
from she.explain import Analysis, Explainer
from she.instructions import Instructions, display_path, edit_file
from she.models import Explanation, LlmAnswer, Summary
from she.parser import split_statements
from she.paths import Paths
from she.render import (
    SECTIONS_ALL,
    SummaryPrinter,
    deterministic_text,
    explanation_json,
    footer,
    inferred_text,
    json_line,
    terminal_width,
)

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_USAGE = 2
EXIT_PARSE = 3
EXIT_LLM = 4
DEFAULT_TIMEOUT_SECONDS = 30.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="she",
        description="Explain a shell command in plain English.",
        usage="she [OPTIONS] -- <command string>\n       she [OPTIONS] < commands.txt",
        epilog="Use `--` before the command so its flags are never read as she flags.",
    )
    parser.add_argument("command", nargs="*", help="the command to explain (put it after --)")
    parser.add_argument("--version", action="version", version=f"she {__version__}")

    explain = parser.add_argument_group("explain options")
    explain.add_argument("--parse-only", action="store_true", help="deterministic layer only; no LLM call")
    explain.add_argument("--list", action="store_true", help="only the files section (visible + inferred)")
    explain.add_argument("--list-env", action="store_true", help="only the env section (visible + inferred)")
    output = explain.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="machine-readable output, printed once when complete")
    output.add_argument("--json-stream", action="store_true", help="NDJSON events: deterministic, summary chunks, done")
    explain.add_argument("--no-learn", action="store_true", help="never run help/version probes for unknown tools")
    explain.add_argument("--no-cache", action="store_true", help="skip the answer cache (no read, no write)")
    explain.add_argument("--model", metavar="NAME", help="Ollama model (default: $SHE_MODEL or qwen3.5:4b)")
    explain.add_argument("--instructions", metavar="FILE", type=Path, help="ad-hoc instructions file for this call")
    explain.add_argument(
        "--timeout", metavar="SEC", type=float, default=DEFAULT_TIMEOUT_SECONDS, help="overall deadline (default 30)"
    )

    pages = parser.add_argument_group("help-page cache")
    pages.add_argument("--parse-cli", metavar="TOOL", help="learn TOOL now: version + recursive help crawl")
    pages.add_argument("--depth", type=int, default=None, help="subcommand depth for --parse-cli (default 3)")
    pages.add_argument("--has-pages", metavar="TOOL", help="show stored and installed versions of TOOL")
    pages.add_argument("--forget", metavar="TOOL[@VERSION]", help="delete stored pages for a tool")
    pages.add_argument("--list-tools", action="store_true", help="list learned tools and versions")

    server = parser.add_argument_group("server")
    server.add_argument("--warm", action="store_true", help="ensure Ollama runs and the model is loaded")
    server.add_argument("--status", action="store_true", help="show Ollama, model and cache status")
    server.add_argument("--clear-cache", action="store_true", help="delete the answer cache")
    server.add_argument("--install-watchdog", action="store_true", help="install the idle-server launchd agent")
    server.add_argument("--uninstall-watchdog", action="store_true", help="remove the idle-server launchd agent")
    server.add_argument("--watchdog-tick", action="store_true", help=argparse.SUPPRESS)

    instructions = parser.add_argument_group("instructions")
    instructions.add_argument("--edit-global-instructions", action="store_true", help="edit ~/.config/she/instructions.md in vim")
    instructions.add_argument("--edit-local-instructions", action="store_true", help="edit ./.she/instructions.md in vim")
    instructions.add_argument("--show-instructions", action="store_true", help="show which instructions would be used")
    return parser


ACTION_FLAGS: Tuple[str, ...] = (
    "parse_cli",
    "has_pages",
    "forget",
    "list_tools",
    "warm",
    "status",
    "clear_cache",
    "install_watchdog",
    "uninstall_watchdog",
    "watchdog_tick",
    "edit_global_instructions",
    "edit_local_instructions",
    "show_instructions",
)


def selected_sections(args: argparse.Namespace) -> Tuple[str, ...]:
    """Sections to output: all, or only files/env with --list/--list-env."""
    if not (args.list or args.list_env):
        return SECTIONS_ALL
    return tuple(name for name, flag in (("files", args.list), ("env", args.list_env)) if flag)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point. Returns the process exit code (contract §8)."""
    parser = build_parser()
    args = parser.parse_args(argv)
    actions = [name for name in ACTION_FLAGS if getattr(args, name)]
    if len(actions) > 1:
        parser.error(f"choose one action at a time, got {', '.join('--' + name.replace('_', '-') for name in actions)}")
    if args.depth is not None and not args.parse_cli:
        parser.error("--depth only applies to --parse-cli")
    if args.depth is not None and args.depth < 0:
        parser.error(f"--depth must be >= 0, got {args.depth}")
    if args.timeout <= 0:
        parser.error(f"--timeout must be positive, got {args.timeout}")
    if actions and args.command:
        parser.error(f"--{actions[0].replace('_', '-')} does not take a command to explain")
    paths = Paths.from_env()
    try:
        match actions:
            case [action]:
                return run_action(action, args, paths, sys.stdout)
            case _:
                return run_explain(args, parser, paths, sys.stdin, sys.stdout)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return EXIT_OK


# -- actions -----------------------------------------------------------------------------------

def run_action(action: str, args: argparse.Namespace, paths: Paths, out: TextIO) -> int:
    match action:
        case "edit_global_instructions" | "edit_local_instructions":
            target = paths.global_instructions if action == "edit_global_instructions" else Paths.local_instructions(Path.cwd())
            try:
                return edit_file(target)
            except FileNotFoundError as error:
                print(f"she: {error}", file=sys.stderr)
                return EXIT_USAGE
        case "show_instructions":
            return show_instructions(args, paths, out)
        case "parse_cli" | "has_pages" | "forget" | "list_tools":
            return run_pages_action(action, args, paths, out)
        case "warm":
            return run_warm(args, paths, out)
        case "status":
            return run_status(args, paths, out)
        case "clear_cache":
            from she.answer_cache import AnswerCache

            existed = AnswerCache.from_paths(paths).clear()
            out.write(f"answer cache {'deleted' if existed else 'was already empty'}: {display_path(paths.answers_db)}\n")
            return EXIT_OK
        case "install_watchdog":
            from she import watchdog

            executable = shutil.which("she") or os.path.abspath(sys.argv[0])
            try:
                plist = watchdog.install(executable, os.environ)
            except RuntimeError as error:
                print(f"she: {error}", file=sys.stderr)
                return EXIT_INTERNAL
            out.write(f"watchdog installed: {display_path(plist)} (runs {executable} --watchdog-tick every 5 min)\n")
            return EXIT_OK
        case "uninstall_watchdog":
            from she import watchdog

            existed = watchdog.uninstall()
            out.write("watchdog removed\n" if existed else "watchdog was not installed\n")
            return EXIT_OK
        case "watchdog_tick":
            from she import watchdog

            watchdog.tick(paths, os.environ)
            return EXIT_OK
        case _:
            raise ValueError(f"unknown action {action=}")


def show_instructions(args: argparse.Namespace, paths: Paths, out: TextIO) -> int:
    try:
        chosen = Instructions.resolve(args.instructions, os.environ, Path.cwd(), paths)
    except FileNotFoundError as error:
        print(f"she: {error}", file=sys.stderr)
        return EXIT_USAGE
    where = display_path(chosen.path) if chosen.path else ("$SHE_INSTRUCTIONS (text)" if chosen.source == "ad-hoc" else "-")
    out.write(f"source: {chosen.source}\npath:   {where}\n")
    if chosen.source != "none":
        out.write("text:\n" + (chosen.text if chosen.text.strip() else "(empty)") + ("\n" if not chosen.text.endswith("\n") else ""))
    return EXIT_OK


def run_pages_action(action: str, args: argparse.Namespace, paths: Paths, out: TextIO) -> int:
    from she.helppages import DEFAULT_DEPTH, HelpLearner
    from she.sandbox import Sandbox
    from she.store import PageStore

    store = PageStore.from_path(paths.pages_db)
    learner = HelpLearner.from_env(store, Sandbox.from_system())
    match action:
        case "parse_cli":
            try:
                report = learner.crawl(args.parse_cli, depth=args.depth if args.depth is not None else DEFAULT_DEPTH)
            except ValueError as error:
                print(f"she: {error}", file=sys.stderr)
                return EXIT_USAGE
            more = " (stopped at the page limit)" if report.truncated else ""
            out.write(
                f"{report.tool} {report.version} ({report.path}): {report.stored} pages stored, "
                f"{report.missing} without help{more}\n"
            )
        case "has_pages":
            report = learner.report(args.has_pages)
            installed = report.installed
            out.write(
                f"{report.tool:<4} installed: "
                + (f"{installed.version} ({installed.path})" if installed else "not on PATH")
                + "\n"
            )
            for index, stored in enumerate(report.stored):
                label = "stored:   " if index == 0 else "          "
                current = "   ← current" if installed and stored.version == installed.version else ""
                day = time.strftime("%Y-%m-%d", time.localtime(stored.learned_at))
                out.write(f"{'':<4} {label} {stored.version:<8} {stored.page_count:>4} pages  {day}{current}\n")
            if not report.stored:
                out.write(f"{'':<4} stored:    none\n")
        case "forget":
            name, _, version = args.forget.partition("@")
            deleted = store.forget(name, version or None)
            out.write(f"forgot {name}{'@' + version if version else ''}: {deleted} pages deleted\n")
        case "list_tools":
            versions = store.stored_versions()
            if not versions:
                out.write("no learned tools\n")
            for stored in versions:
                day = time.strftime("%Y-%m-%d", time.localtime(stored.learned_at))
                out.write(f"{stored.name:<16} {stored.version:<16} {stored.page_count:>4} pages  {day}  {stored.path}\n")
    return EXIT_OK


def run_warm(args: argparse.Namespace, paths: Paths, out: TextIO) -> int:
    from she.llm import LlmError, OllamaClient, OllamaConfig

    client = OllamaClient(OllamaConfig.from_env(args.model), paths)
    deadline = time.monotonic() + max(args.timeout, 120.0)
    try:
        started = client.ensure_running(deadline)
        digest = client.model_digest(deadline)
        client.warm(deadline)
    except LlmError as error:
        print(f"she: {error}", file=sys.stderr)
        return EXIT_LLM
    out.write(f"ollama {'started' if started else 'running'}; {client.config.model}@{digest[:12]} loaded\n")
    return EXIT_OK


def _size(path: Path) -> str:
    if not path.exists():
        return "absent"
    size = path.stat().st_size
    return f"{size / 1024:.0f} KB" if size < 1024 * 1024 else f"{size / 1024 / 1024:.1f} MB"


def run_status(args: argparse.Namespace, paths: Paths, out: TextIO) -> int:
    from she import watchdog
    from she.llm import LlmError, OllamaClient, OllamaConfig

    config = OllamaConfig.from_env(args.model)
    client = OllamaClient(config, paths)
    version = client.server_version()
    out.write(f"ollama:        {'reachable at ' + config.base_url + ' (v' + version + ')' if version else 'not reachable at ' + config.base_url}\n")
    digest_text = "unknown (server not reachable)"
    loaded = "unknown"
    if version:
        try:
            digest_text = client.model_digest()
        except LlmError as error:
            digest_text = str(error)
        try:
            names = [model.get("name", "") for model in client.running_models()]
            loaded = "yes" if config.model in names or f"{config.model}:latest" in names else "no"
        except LlmError:
            loaded = "unknown"
    out.write(f"model:         {config.model}\ndigest:        {digest_text}\nloaded:        {loaded}\n")
    out.write(f"keep_alive:    {config.keep_alive}\n")
    out.write(f"pages cache:   {display_path(paths.pages_db)} ({_size(paths.pages_db)})\n")
    out.write(f"answer cache:  {display_path(paths.answers_db)} ({_size(paths.answers_db)})\n")
    out.write(f"machine key:   {display_path(paths.key_file)} ({'present' if paths.key_file.exists() else 'absent'})\n")
    pid = watchdog.read_pid(paths.ollama_pid_file)
    out.write(f"she-started server pid: {pid if pid is not None else 'none'}\n")
    out.write(f"watchdog:      {'installed' if watchdog.plist_path().exists() else 'not installed'}\n")
    return EXIT_OK


# -- explain -----------------------------------------------------------------------------------

def read_commands(args: argparse.Namespace, parser: argparse.ArgumentParser, stdin: TextIO) -> Tuple[List[str], bool]:
    """Commands to explain and whether they came from stdin (then JSON output is a list)."""
    if args.command:
        command = " ".join(args.command)
        if not command.strip():
            parser.error("the command to explain is empty")
        return [command], False
    if stdin.isatty():
        parser.error("no command given; use: she -- '<command>'  (or pipe commands on stdin)")
    statements = split_statements(stdin.read())
    if not statements:
        parser.error("no command found on stdin")
    return statements, True


@dataclass(frozen=True, slots=True)
class LlmOutcome:
    """Result of the LLM step for one command."""

    answer: Optional[LlmAnswer] = None
    summary: Optional[Summary] = None
    model: Optional[str] = None
    llm_ms: int = 0
    error: Optional[str] = None

    def copy(self, **changes: object) -> "LlmOutcome":
        return replace(self, **changes)


def run_llm(
    analysis: Analysis,
    instructions: Instructions,
    args: argparse.Namespace,
    paths: Paths,
    deadline: float,
    on_chunk: Callable[[str], None],
) -> LlmOutcome:
    """Ask the model (or the cache) for the summary and inferred effects; never raises LlmError."""
    from she.answer_cache import AnswerCache, CacheKeyParts
    from she.llm import AnswerStream, LlmError, OllamaClient, OllamaConfig, without_visible
    from she.prompt import PROMPT_TEMPLATE_VERSION, build_messages

    config = OllamaConfig.from_env(args.model)
    client = OllamaClient(config, paths)
    started = time.perf_counter()
    model_label: Optional[str] = None
    try:
        client.ensure_running(deadline)
        digest = client.model_digest(deadline)
        model_label = f"{config.model}@{digest}"
        cache = None if args.no_cache else AnswerCache.from_paths(paths)
        key = (
            cache.key(
                CacheKeyParts(
                    template_version=PROMPT_TEMPLATE_VERSION,
                    command=analysis.command,
                    model_digest=digest,
                    facts_text=analysis.facts_text,
                    instructions_text=instructions.text,
                    num_predict=config.num_predict,
                )
            )
            if cache is not None
            else None
        )
        cached = cache.get(key) if cache is not None and key is not None else None
        if cached is not None:
            on_chunk(cached.summary)
            return LlmOutcome(
                answer=cached, summary=Summary(text=cached.summary, model=model_label, cache="hit"), model=model_label
            )
        stream = AnswerStream()
        for chunk in client.chat_stream(build_messages(analysis.command, analysis.facts_text, instructions.text), deadline):
            released = stream.feed(chunk)
            if released:
                on_chunk(released)
        tail, raw_answer = stream.finish()
        if tail:
            on_chunk(tail)
        answer = without_visible(raw_answer, analysis.files, analysis.env)
        if cache is not None and key is not None:
            cache.put(key, answer, digest)
        return LlmOutcome(
            answer=answer,
            summary=Summary(text=answer.summary, model=model_label, cache="off" if cache is None else "miss"),
            model=model_label,
            llm_ms=round((time.perf_counter() - started) * 1000),
        )
    except LlmError as error:
        return LlmOutcome(model=model_label, llm_ms=round((time.perf_counter() - started) * 1000), error=str(error))


def build_explanation(analysis: Analysis, instructions: Instructions, outcome: Optional[LlmOutcome]) -> Explanation:
    answer = outcome.answer if outcome is not None else None
    timing = dict(analysis.timings)
    if outcome is not None:
        timing["llm"] = outcome.llm_ms
    warnings = list(analysis.warnings)
    if outcome is not None and outcome.error:
        warnings.append(f"LLM unavailable: {outcome.error}")
    return Explanation(
        command=analysis.command,
        summary=outcome.summary if outcome is not None else None,
        instructions=instructions.info,
        breakdown=analysis.breakdown,
        files=(*analysis.files, *(answer.files if answer else ())),
        env=(*analysis.env, *(answer.env if answer else ())),
        unknowns=analysis.unknowns,
        warnings=tuple(warnings),
        timing_ms=timing,
    )


def run_explain(
    args: argparse.Namespace, parser: argparse.ArgumentParser, paths: Paths, stdin: TextIO, out: TextIO
) -> int:
    commands, from_stdin = read_commands(args, parser, stdin)
    deadline = time.monotonic() + args.timeout
    try:
        instructions = Instructions.resolve(args.instructions, os.environ, Path.cwd(), paths)
    except FileNotFoundError as error:
        print(f"she: {error}", file=sys.stderr)
        return EXIT_USAGE
    sections = selected_sections(args)
    explainer = Explainer.from_paths(paths, learn=not args.no_learn)
    width = terminal_width()
    collected: List[dict] = []
    llm_failed = parse_failed = False
    for position, command in enumerate(commands):
        analysis = explainer.analyze(command)
        parse_failed = parse_failed or analysis.has_parse_error
        mode = "json" if args.json else "stream" if args.json_stream else "human"
        if mode == "human" and position > 0:
            out.write("\n" + "─" * min(width, 60) + "\n\n")
        outcome = explain_one(analysis, instructions, args, paths, deadline, sections, mode, width, out)
        llm_failed = llm_failed or (outcome is not None and outcome.error is not None)
        if outcome is not None and outcome.error is not None:
            print(f"she: {outcome.error}", file=sys.stderr)
        if mode == "json":
            collected.append(explanation_json(build_explanation(analysis, instructions, outcome), sections))
    exit_code = EXIT_LLM if llm_failed else EXIT_PARSE if parse_failed else EXIT_OK
    if args.json:
        import json

        payload = collected if from_stdin else collected[0]
        out.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return exit_code


def explain_one(
    analysis: Analysis,
    instructions: Instructions,
    args: argparse.Namespace,
    paths: Paths,
    deadline: float,
    sections: Tuple[str, ...],
    mode: str,
    width: int,
    out: TextIO,
) -> Optional[LlmOutcome]:
    """Print (or emit) one command's output in the given mode; returns the LLM outcome, if any."""
    match mode:
        case "human":
            out.write(
                deterministic_text(
                    analysis.command,
                    analysis.breakdown,
                    analysis.files,
                    analysis.env,
                    analysis.unknowns,
                    analysis.warnings,
                    sections,
                    width,
                )
            )
            out.flush()
            if args.parse_only:
                out.write("\n" + footer(analysis.sources, None, instructions.source).replace("model unavailable", "no LLM (--parse-only)"))
                return None
            printer = SummaryPrinter(out, width)
            show_summary = "summary" in sections
            if show_summary:
                printer.start()
            outcome = run_llm(analysis, instructions, args, paths, deadline, printer.write if show_summary else _ignore)
            if show_summary:
                if outcome.error is not None:
                    printer.write(f"(LLM unavailable: {outcome.error})")
                printer.finish()
            if outcome.answer is not None:
                inferred = inferred_text(outcome.answer.files, outcome.answer.env, sections)
                if inferred:
                    out.write("\n" + inferred)
            out.write("\n" + footer(analysis.sources, outcome.model, instructions.source))
            return outcome
        case "stream":
            event = {
                "event": "deterministic",
                "command": analysis.command,
                "breakdown": [item.model_dump(mode="json") for item in analysis.breakdown],
                "files": [item.model_dump(mode="json") for item in analysis.files],
                "env": [item.model_dump(mode="json") for item in analysis.env],
                "unknowns": [item.model_dump(mode="json") for item in analysis.unknowns],
            }
            out.write(json_line(event))
            out.flush()
            outcome = None
            if not args.parse_only:
                def emit(text: str) -> None:
                    out.write(json_line({"event": "summary_chunk", "text": text}))
                    out.flush()

                outcome = run_llm(analysis, instructions, args, paths, deadline, emit)
                if outcome.answer is not None:
                    out.write(
                        json_line(
                            {
                                "event": "inferred",
                                "files": [item.model_dump(mode="json") for item in outcome.answer.files],
                                "env": [item.model_dump(mode="json") for item in outcome.answer.env],
                            }
                        )
                    )
            explanation = build_explanation(analysis, instructions, outcome)
            exit_code = (
                EXIT_LLM if outcome is not None and outcome.error else EXIT_PARSE if analysis.has_parse_error else EXIT_OK
            )
            out.write(
                json_line(
                    {
                        "event": "done",
                        "summary": explanation.summary.model_dump(mode="json") if explanation.summary else None,
                        "warnings": list(explanation.warnings),
                        "timing_ms": explanation.timing_ms,
                        "exit": exit_code,
                    }
                )
            )
            out.flush()
            return outcome
        case _:
            return None if args.parse_only else run_llm(analysis, instructions, args, paths, deadline, _ignore)


def _ignore(text: str) -> None:
    del text


if __name__ == "__main__":
    sys.exit(main())
