"""
service/actions/base.py

What an action is.

One declaration per action, from which everything else derives: the tool
schema the model is given, the validation applied to what comes back, the
sentence shown for confirmation, and the call that carries it out.

Before this, adding an action meant editing four separate places — a
registry, the prompt text, a resolver with a branch per action, and a
dispatcher with another branch per action. They could disagree, and one of
them usually did.

Now an action is one object in one file, and nothing else changes.
"""

from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass(frozen=True)
class Resolution:
    """
    The outcome of turning the model's arguments into something executable.

    Three outcomes, deliberately distinct. A name that matches two things is
    a question, not a coin toss: guessing is how "run the smoke suite for
    Netra" quietly runs a different application's tests.
    """
    ok: bool = False
    args: dict = field(default_factory=dict)      # resolved, ready to execute
    summary: str = ""                              # what will happen, in words
    question: str = ""                             # ask when ambiguous
    options: list = field(default_factory=list)    # choices for that question
    error: str = ""                                # cannot be done, and why

    @staticmethod
    def done(args: dict, summary: str) -> "Resolution":
        return Resolution(ok=True, args=args, summary=summary)

    @staticmethod
    def ask(question: str, options: list) -> "Resolution":
        return Resolution(question=question, options=options)

    @staticmethod
    def refuse(error: str) -> "Resolution":
        return Resolution(error=error)


@dataclass(frozen=True)
class Action:
    """
    A single thing the assistant is allowed to do.

    Attributes:
        name: Stable identifier. Becomes the tool's function name.
        description: What it does, written for the model. This is the only
            place the model learns what the action is for, so it is prose
            rather than a label.
        parameters: JSON Schema for the arguments. Used verbatim as the tool
            schema AND as the validation contract, so the two cannot drift.
        resolve: Turns model arguments into executable ones — names to ids,
            times to timestamps — using a catalogue of what actually exists.
        execute: Carries it out. Called only after a person confirms.
        confirm_fields: Argument names the user may adjust on the
            confirmation card before agreeing. A schedule's time should be
            adjustable; a suite id should not.
    """
    name: str
    description: str
    parameters: dict
    resolve: Callable[[dict, dict], Resolution]
    execute: Callable[[dict, str], dict]
    confirm_fields: tuple = ()

    def tool_schema(self) -> dict:
        """
        The definition handed to the model.

        Shaped for OpenAI-style function calling, which is what the provider
        layer expects and what Ollama accepts for models that support tools.
        """
        # The provider expects "input_schema" and reads nothing else. Sending
        # "parameters" meant every tool went out with an EMPTY schema — the
        # model got names and descriptions but no arguments to fill in, which
        # looked exactly like the model being unable to call tools.
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }
