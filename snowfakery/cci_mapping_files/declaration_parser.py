import typing as T
from collections import defaultdict
from datetime import date
from pathlib import Path

import yaml
from pydantic import ConfigDict, BaseModel, PositiveInt

# Pydantic 1.x stuff
try:
    from pydantic import field_validator, RootModel
except ImportError:
    # for Pydantic 1.x
    from pydantic import validator
    def field_validator(*args, mode=None):
        assert mode == "before" # only mode supported in this backport
        return validator(*args, pre=True)
    from pydantic import BaseModel
    RootModel = BaseModel


class AtomicDecl(T.NamedTuple):
    """A single object/key/value declaration

    Might be extracted from a larger declaration set.

    e.g.

    - sf_object: foo
      a: b
      c: d

    Implies:
    AtomicDecl("foo", "a", "b", ...)
    AtomicDecl("foo", "c", "d", ...)
    """

    sf_object: str
    key: str
    value: T.Union[T.List, str, date, int]
    priority: int
    merge_rule: T.Callable  # what to do if two declarations for same val


class MergeRules:
    """Namespace for merge rules.

    Merge rules say what to do if there are two rules
    that apply to the same sobject with the same key."""

    @staticmethod
    def use_highest_priority(
        new_decl: T.Optional[AtomicDecl], existing_decl: T.Optional[AtomicDecl]
    ):
        """The Highlander strategy. There can be only one."""
        if existing_decl:
            return max(existing_decl, new_decl, key=lambda decl: decl.priority)
        return new_decl

    @staticmethod
    def append(new_decl: AtomicDecl, existing_decl: T.Optional[AtomicDecl]):
        """The collaborative strategy. Let's work together."""
        if existing_decl:
            existing_decl.value.append(new_decl.value)
            return existing_decl
        else:
            d = new_decl
            # start to build a list-based declaration
            return AtomicDecl(d.sf_object, d.key, [d.value], d.priority, d.merge_rule)


class SObjectRuleDeclaration(BaseModel):
    sf_object: str
    priority: T.Optional[T.Literal["low", "medium", "high"]] = None

    api: T.Optional[T.Literal["smart", "rest", "bulk"]] = None
    batch_size: T.Optional[int] = None
    bulk_mode: T.Optional[T.Literal["serial", "parallel"]] = None
    anchor_date: T.Union[str, date, None] = None

    load_after: T.Optional[str] = None
    model_config = ConfigDict(extra="forbid")

    @property
    def priority_number(self):
        values = {"low": 1, "medium": 2, "high": 3, None: 2}
        return values[self.priority]

    @field_validator("priority", "api", "bulk_mode", mode="before")
    @classmethod
    def case_normalizer(cls, val):
        if hasattr(val, "lower"):
            return val.lower()
        else:
            return val

    def as_mapping(self):
        rc = {
            "api": self.api,
            "bulk_mode": self.bulk_mode,
            "batch_size": self.batch_size,
            "anchor_date": self.anchor_date,
        }
        return {k: v for k, v in rc.items() if v is not None}


MERGE_RULES = {
    "api": MergeRules.use_highest_priority,
    "bulk_mode": MergeRules.use_highest_priority,
    "batch_size": MergeRules.use_highest_priority,
    "anchor_date": MergeRules.use_highest_priority,
    "load_after": MergeRules.append,
}


class ChannelDeclaration(BaseModel):
    "Channel declarations are only of relevance to Salesforce employees"
    user: str
    recipe_options: T.Optional[T.Dict[str, T.Any]] = None
    num_generators: T.Optional[PositiveInt] = None
    num_loaders: T.Optional[PositiveInt] = None
    model_config = ConfigDict(extra="forbid")


class ChannelDeclarationList(BaseModel):
    "Channel declarations are only of relevance to Salesforce employees"
    user_channels: T.List[ChannelDeclaration]


class LoadDeclarationsTuple(T.NamedTuple):
    sobject_declarations: T.List[SObjectRuleDeclaration]
    channel_declarations: T.List[
        ChannelDeclaration
    ]  # Channel declarations are only of relevance to Salesforce employees


class SObjectRuleDeclarationFile(RootModel):
    if RootModel==BaseModel:
        # for Pydantic 1.x
        __root__: T.List[T.Union[ChannelDeclarationList, SObjectRuleDeclaration]]
    else:
        root: T.List[T.Union[ChannelDeclarationList, SObjectRuleDeclaration]]
    

    @classmethod
    def parse_from_yaml(cls, f: T.Union[Path, T.TextIO]):
        "Parse from a file-like or Path"
        if isinstance(f, Path):
            with open(f) as fd:
                data = yaml.safe_load(fd)
        else:
            data = yaml.safe_load(f)
        print(RootModel, BaseModel, BaseModel.model_validate)
        sobject_decls = [
            obj
            for obj in cls.model_validate(data).root
            if isinstance(obj, SObjectRuleDeclaration)
        ]
        channel_decls = [
            obj
            for obj in cls.model_validate(data).root
            if isinstance(obj, ChannelDeclarationList)
        ]
        if len(channel_decls) > 1:
            raise AssertionError("Only one channel declaration list allowed per file.")
        elif len(channel_decls) == 1:
            channels = channel_decls[0].user_channels
        else:
            channels = []

        return LoadDeclarationsTuple(sobject_decls, channels)


def atomize_decls(decls: T.Sequence[SObjectRuleDeclaration]):
    rc = []
    for decl in decls:
        for key, merge_rule in MERGE_RULES.items():
            val = getattr(decl, key)
            if val is not None:
                rc.append(
                    AtomicDecl(
                        decl.sf_object, key, val, decl.priority_number, merge_rule
                    )
                )
    return rc


def merge_declarations(decls: T.Sequence[SObjectRuleDeclaration]):
    atomic_decls = atomize_decls(decls)
    results = defaultdict(dict)
    for decl in atomic_decls:
        sobject, key, value, priority_number, merge_rule = decl
        previous_decl = results[decl.sf_object].get(decl.key)
        results[sobject][key] = merge_rule(decl, previous_decl)
    return results


def unify(decls: T.Sequence[SObjectRuleDeclaration]):
    decls = merge_declarations(decls)
    objs = {}
    for sobj, sobjdecls in decls.items():
        unified_decls = objs[sobj] = SObjectRuleDeclaration(sf_object=sobj)
        for key, decl in sobjdecls.items():
            assert decl.key == key
            setattr(unified_decls, key, decl.value)
    return objs
