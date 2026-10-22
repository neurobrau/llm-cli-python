"""Tests for in-memory conversation history."""

from neurobrau_cli.conversation import Conversation, Message


def test_new_conversation_is_empty() -> None:
    assert Conversation().messages == ()


def test_commit_exchange_appends_messages_in_order() -> None:
    conversation = Conversation()

    conversation.commit_exchange("Hi", "Hello!")
    conversation.commit_exchange("How are you?", "Fine.")

    assert conversation.messages == (
        Message("user", "Hi"),
        Message("assistant", "Hello!"),
        Message("user", "How are you?"),
        Message("assistant", "Fine."),
    )


def test_with_user_message_does_not_commit() -> None:
    conversation = Conversation()
    conversation.commit_exchange("Hi", "Hello!")

    pending = conversation.with_user_message("Next")

    assert pending == [
        Message("user", "Hi"),
        Message("assistant", "Hello!"),
        Message("user", "Next"),
    ]
    assert len(conversation.messages) == 2


def test_reset_clears_history() -> None:
    conversation = Conversation()
    conversation.commit_exchange("Hi", "Hello!")

    conversation.reset()

    assert conversation.messages == ()
