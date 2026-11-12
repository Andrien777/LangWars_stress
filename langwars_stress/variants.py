"""Per-player code variants, shared-name shadowing and core overrides.

Every player gets a code book: logical identifiers from the reference program are
replaced with distinct, dictionary-valid names. Players are allocated in small
groups that share one name block and rotate it, so submissions intentionally
shadow each other's globals. Names are never repeated inside one program.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .solutions import TASK_SOLUTIONS, correct_source, task_key, wrong_source

RESERVED_WORDS = {
    "fn", "let", "return", "if", "else", "while", "comefrom", "and", "or", "not",
    "solution", "float", "function", "int", "string", "list", "dict",
    "eq", "cmp", "get", "set", "length", "contains", "empty", "push_back",
    "truthy", "concat", "cast",
    "__builtin_eq__", "__builtin_lt__", "__builtin_get__", "__builtin_set__",
    "__builtin_length__", "__builtin_concat__",
}

# Used only when the deployment has no dictionary policy (allowedWords is null).
FALLBACK_WORDS = """
alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron
pi rho sigma tau upsilon phi chi psi omega able brave calm clear kind light
quick safe silent small solid steady strong swift tidy warm wise young bright
common simple gentle happy honest local major minor modern natural normal open
plain quiet ready round sharp short soft square stable still sweet true usual
fresh green clean fair fine good great large little long new old right special
""".split()

OVERRIDE_CANDIDATES = ("eq", "get", "concat")
OVERRIDE_SLOTS = 3  # function name plus two parameters


def _without_comments_and_strings(source: str) -> str:
    source = re.sub(r"/\*.*?\*/", " ", source, flags=re.DOTALL)
    source = re.sub(r"//[^\n]*", " ", source)
    source = re.sub(r'"(?:\\.|[^"\\])*"', '""', source)
    return source


def code_identifiers(source: str) -> list[str]:
    text = _without_comments_and_strings(source)
    found = {match.group(0) for match in re.finditer(r"[A-Za-z]+(?:_[A-Za-z]+)*", text)}
    return sorted(
        name for name in found
        if name not in RESERVED_WORDS and "builtin" not in name
    )


def task_identifiers(task: dict) -> list[str]:
    correct = code_identifiers(correct_source(task))
    wrong = code_identifiers(wrong_source(task))
    return sorted(set(correct) | set(wrong))


class NameAllocator:
    """Deterministic base-N names built from short allowed dictionary words."""

    def __init__(self, words, max_length: int, needed: int):
        useful = sorted(
            {
                str(word).lower() for word in words
                if re.fullmatch(r"[a-z]+", str(word).lower())
                and str(word).lower() not in RESERVED_WORDS
            },
            key=lambda word: (len(word), word),
        )
        if not useful:
            raise ValueError("the allowed-word dictionary has no usable short words")
        needed = max(1, needed)
        self.words: list[str] = []
        self.width = 2
        for width in (2, 3, 4):
            usable = [word for word in useful if len(word) * width + (width - 1) <= max_length]
            if len(usable) >= 2 and len(usable) ** width >= needed * 2:
                self.words = usable
                self.width = width
                break
        if not self.words:
            raise ValueError(
                f"cannot build {needed} distinct valid names with maxNameLength={max_length}; "
                "reduce --players or add shorter words to the dictionary"
            )
        self.max_length = max_length
        self.index = 0
        self.used: set[str] = set()

    def _at(self, index: int) -> str:
        parts = []
        for _ in range(self.width):
            parts.append(self.words[index % len(self.words)])
            index //= len(self.words)
        return "_".join(parts)

    def take(self, count: int) -> list[str]:
        result = []
        guard = 0
        while len(result) < count:
            guard += 1
            if guard > count * 100 + 1000:
                raise ValueError("the allowed-word dictionary cannot provide enough distinct names")
            name = self._at(self.index)
            self.index += 1
            if name in self.used or name in RESERVED_WORDS or len(name) > self.max_length:
                continue
            self.used.add(name)
            result.append(name)
        return result


def _common_words(tasks: list[dict], global_words) -> list[str]:
    sources = []
    unrestricted = False
    for task in tasks:
        raw = task.get("allowedWords")
        if raw is None:
            raw = global_words
        if raw is None:
            unrestricted = True
            continue
        sources.append({str(word).lower() for word in raw})
    if not sources:
        return list(FALLBACK_WORDS)
    common = set.intersection(*sources)
    if not common:
        raise ValueError("the selected tasks share no allowed identifier words")
    if unrestricted:
        # At least one task has no dictionary policy; the common words are still
        # valid everywhere, so use them rather than the unconstrained fallback.
        return sorted(common)
    return sorted(common)


def _replace_identifiers(source: str, mapping: dict[str, str]) -> str:
    if not mapping:
        return source
    keys = sorted(mapping, key=len, reverse=True)
    pattern = re.compile(r"\b(?:" + "|".join(re.escape(key) for key in keys) + r")\b")
    return pattern.sub(lambda match: mapping[match.group(0)], source)


@dataclass
class PlayerCode:
    task_maps: dict[str, dict[str, str]]
    override_names: dict[str, tuple[str, str, str]]

    def variant(self, task: dict) -> str:
        return _replace_identifiers(correct_source(task), self.task_maps.get(task_key(task), {}))

    def wrong(self, task: dict) -> str:
        return _replace_identifiers(wrong_source(task), self.task_maps.get(task_key(task), {}))

    def override(self, operation: str) -> str:
        names = self.override_names.get(operation)
        if names is None:
            raise ValueError(f"unsupported override operation: {operation}")
        name, first, second = names
        if operation == "eq":
            return f"@eq fn <int,int> {name}({first},{second}){{return __builtin_eq__({first},{second});}}\n"
        if operation == "get":
            return f"@get fn <list,int> {name}({first},{second}){{return __builtin_get__({first},{second});}}\n"
        if operation == "concat":
            return f"@concat fn <string,string> {name}({first},{second}){{return __builtin_concat__({first},{second});}}\n"
        raise ValueError(f"unsupported override operation: {operation}")

    def with_override(self, task: dict, operation: str) -> str:
        return self.override(operation) + self.variant(task)


class CodeBook:
    def __init__(
        self,
        tasks: list[dict],
        *,
        allowed_words,
        max_name_length: int | None,
        players: int,
        group_size: int = 4,
    ):
        self.group_size = max(1, group_size)
        self.task_identifiers = {task_key(task): task_identifiers(task) for task in tasks}
        self.max_slots = max((len(names) for names in self.task_identifiers.values()), default=0)
        if self.max_slots == 0:
            raise ValueError("the selected tasks expose no user-defined identifiers to vary")
        words = _common_words(tasks, allowed_words)
        if not words:
            words = FALLBACK_WORDS
        max_length = int(max_name_length or 48)
        if max_length < 3:
            raise ValueError("maxNameLength is too small to generate per-player identifiers")
        blocks = math.ceil(max(1, players) / self.group_size)
        # Rotate across at least the group size so even a one-identifier task
        # differs between players in the same shadow group.
        self.template_slots = max(self.max_slots, self.group_size, 2)
        override_slots = OVERRIDE_SLOTS * len(OVERRIDE_CANDIDATES)

        # Override definitions must not accidentally use a name that a template
        # has already published as a variable. Keep them in a separate namespace
        # with a marker word that templates never use. This survives repeated
        # runs, where old template variables remain in the shared world.
        reference_parts = {
            part for names in self.task_identifiers.values()
            for name in names for part in name.split("_")
        }
        marker_candidates = sorted(
            (
                word for word in words
                if word not in reference_parts
                and word not in RESERVED_WORDS
                and len(word) + 2 <= max_length - 2
            ),
            key=lambda word: (-len(word), word),
        )
        marker = marker_candidates[0] if marker_candidates else None
        try:
            if marker:
                template_words = [word for word in words if word != marker]
                template_allocator = NameAllocator(template_words, max_length, blocks * self.template_slots)
                self.template_blocks = [
                    template_allocator.take(self.template_slots) for _ in range(blocks)
                ]
                inner_allocator = NameAllocator(
                    template_words,
                    max_length - len(marker) - 1,
                    blocks * override_slots,
                )
                flat_overrides = [
                    marker + "_" + inner for inner in inner_allocator.take(blocks * override_slots)
                ]
            else:
                allocator = NameAllocator(words, max_length, blocks * (self.template_slots + override_slots))
                self.template_blocks = [
                    allocator.take(self.template_slots) for _ in range(blocks)
                ]
                flat_overrides = allocator.take(blocks * override_slots)
        except ValueError:
            # A very small dictionary cannot reserve a marker; fall back to one
            # shared allocator (still disjoint within this run).
            allocator = NameAllocator(words, max_length, blocks * (self.template_slots + override_slots))
            self.template_blocks = [
                allocator.take(self.template_slots) for _ in range(blocks)
            ]
            flat_overrides = allocator.take(blocks * override_slots)
        self.override_blocks = [
            flat_overrides[index * override_slots:(index + 1) * override_slots]
            for index in range(blocks)
        ]

    def player(self, index: int) -> PlayerCode:
        block_index = index // self.group_size
        shift = index % self.group_size
        block = self.template_blocks[block_index]
        task_maps = {}
        for key, identifiers in self.task_identifiers.items():
            task_maps[key] = {
                identifier: block[(position + shift) % self.template_slots]
                for position, identifier in enumerate(identifiers)
            }
        overrides = self.override_blocks[block_index]
        override_names = {}
        for operation_index, operation in enumerate(OVERRIDE_CANDIDATES):
            base = operation_index * OVERRIDE_SLOTS
            override_names[operation] = tuple(
                overrides[base + ((shift + offset) % OVERRIDE_SLOTS)]
                for offset in range(OVERRIDE_SLOTS)
            )
        return PlayerCode(task_maps, override_names)
