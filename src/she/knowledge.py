"""Built-in knowledge: shell builtins, operators, redirects and command wrappers."""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

BUILTINS: Mapping[str, str] = MappingProxyType(
    {
        ":": "no-op builtin: does nothing and succeeds",
        ".": "run a file's commands in the current shell",
        "source": "run a file's commands in the current shell",
        "alias": "define or list command aliases",
        "unalias": "remove command aliases",
        "bg": "resume a stopped job in the background",
        "fg": "bring a job to the foreground",
        "builtin": "run a shell builtin, bypassing functions",
        "cd": "change the current directory",
        "command": "run a command, bypassing shell functions",
        "declare": "declare shell variables and set their attributes",
        "typeset": "declare shell variables and set their attributes",
        "local": "declare variables local to the current function",
        "readonly": "mark shell variables as read-only",
        "dirs": "show the directory stack",
        "disown": "detach jobs from the shell",
        "echo": "print its arguments",
        "eval": "run its arguments as a shell command (contents not analysed)",
        "exec": "replace the shell with the given command",
        "exit": "exit the shell",
        "export": "set shell variables and export them to child processes",
        "false": "do nothing and fail",
        "true": "do nothing and succeed",
        "getopts": "parse positional parameters as options",
        "hash": "remember or report command locations",
        "history": "show or edit the command history",
        "jobs": "list background jobs",
        "kill": "send a signal to a process",
        "let": "evaluate arithmetic expressions",
        "popd": "pop a directory off the stack and cd to it",
        "pushd": "push a directory on the stack and cd to it",
        "printf": "print formatted text",
        "pwd": "print the current directory",
        "read": "read a line from stdin into variables",
        "return": "return from a shell function",
        "set": "set shell options or positional parameters",
        "shift": "shift positional parameters",
        "shopt": "set bash shell options",
        "test": "evaluate a condition",
        "[": "evaluate a condition",
        "[[": "evaluate a condition (bash extended test)",
        "trap": "run a command when the shell receives a signal",
        "type": "describe how a name would be interpreted",
        "ulimit": "show or set resource limits",
        "umask": "show or set the file-creation mask",
        "unset": "remove shell variables or functions",
        "wait": "wait for background jobs to finish",
    }
)

KEYWORDS: Mapping[str, str] = MappingProxyType(
    {
        "if": "run the following branch only if the condition succeeds",
        "for": "loop over a list of words",
        "while": "loop while the condition succeeds",
        "until": "loop until the condition succeeds",
        "case": "match a word against patterns",
        "select": "show a numbered menu and loop over the choice",
        "!": "negate the exit status of the following command",
    }
)

OPERATORS: Mapping[str, str] = MappingProxyType(
    {
        "|": "pipe: send the left command's output into the right command",
        "|&": "pipe both output and errors into the right command",
        "&&": "run the right side only if the left side succeeds",
        "||": "run the right side only if the left side fails",
        ";": "run the commands one after another",
        "&": "run the preceding command in the background",
    }
)

REDIRECTS: Mapping[str, str] = MappingProxyType(
    {
        ">": "write output to a file (overwrites it)",
        ">|": "write output to a file, overwriting even with noclobber",
        ">>": "append output to a file",
        "<": "read input from a file",
        "&>": "write output and errors to a file (overwrites it)",
        "&>>": "append output and errors to a file",
        ">&": "redirect this output to another file descriptor",
        "<&": "read input from another file descriptor",
        "<<": "here-document: feed the following lines as input",
        "<<-": "here-document: feed the following lines as input",
        "<<<": "here-string: feed this word as input",
        "<>": "open a file for reading and writing",
    }
)

SUBSTITUTIONS: Mapping[str, str] = MappingProxyType(
    {
        "$(": "command substitution: run the inner command and insert its output here",
        "`": "command substitution: run the inner command and insert its output here",
        "<(": "process substitution: pass the inner command's output as a file",
        ">(": "process substitution: pass a file that feeds the inner command",
    }
)

# Commands that run another command. Values are the wrapper's options that take a value.
WRAPPERS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "sudo": frozenset({"-u", "-g", "-C", "-p", "-h", "-U", "-r", "-t", "-D", "-T"}),
        "doas": frozenset({"-u", "-C"}),
        "env": frozenset({"-u", "-S", "-C", "-P"}),
        "nohup": frozenset(),
        "time": frozenset({"-o", "-f"}),
        "nice": frozenset({"-n"}),
        "timeout": frozenset({"-s", "-k", "--signal", "--kill-after"}),
        "gtimeout": frozenset({"-s", "-k", "--signal", "--kill-after"}),
        "command": frozenset(),
        "exec": frozenset({"-a"}),
        "xargs": frozenset({"-I", "-J", "-n", "-P", "-L", "-s", "-d", "-E", "-R", "-S"}),
        "caffeinate": frozenset({"-t", "-w"}),
        "watch": frozenset({"-n", "-d"}),
        "arch": frozenset(),
    }
)

# Wrappers whose first positional argument is not the wrapped command.
WRAPPER_LEADING_POSITIONALS: Mapping[str, int] = MappingProxyType({"timeout": 1, "gtimeout": 1})

SHELLS_WITH_INLINE_SCRIPT = frozenset({"bash", "sh", "zsh", "dash", "ksh", "fish"})

QUIET_TARGETS = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"})
