from integrations.intake_routing import normalize_analysis


EMAIL_REQUEST = (
    "Prepare an email to person@example.com with the subject "
    "'Agent Integration Test'. The body should say: "
    "'This is a test email from my Agentic Work Intake application. "
    "I am verifying the Gmail integration.' Show me the proposed email first. "
    "Do not send it until I explicitly approve it."
)


def analysis_for(tool, parameters, *, category="requires_clarification", missing=None):
    return {
        "task_title": "Draft Email to my email address",
        "summary": "Prepare an email for review.",
        "priority": "Medium",
        "detected_deadline": "None detected",
        "missing_information": missing or [],
        "actions": [
            {
                "action_title": "Draft Email for Review",
                "tool_to_use": tool,
                "routing_category": category,
                "reason": "",
                "parameters": parameters,
            }
        ],
    }


def test_email_request_does_not_require_unrelated_fields_or_change_content():
    recipient = "person@example.com"
    subject = "Agent Integration Test"
    body = (
        "This is a test email from my Agentic Work Intake application. "
        "I am verifying the Gmail integration."
    )
    result = normalize_analysis(
        analysis_for(
            "draft_communication",
            {
                "recipient": recipient,
                "subject": subject,
                "body": body,
            },
            missing=[
                "url",
                "title",
                "deadline",
                "priority",
                "description",
                "due_date",
                "recipient",
                "subject",
                "body",
            ],
        ),
        raw_text=EMAIL_REQUEST,
    )

    assert result["missing_information"] == []
    action = result["actions"][0]
    assert action["tool_to_use"] == "send_email"
    assert action["routing_category"] == "prepare_for_human_review"
    assert action["parameters"] == {
        "recipient": recipient,
        "subject": subject,
        "body": body,
    }


def test_multiple_recipients_are_normalized_before_approval():
    result = normalize_analysis(
        analysis_for(
            "send_email",
            {
                "recipient": " first@example.com , second@EXAMPLE.COM ",
                "subject": "Status",
                "body": "Ready.",
            },
        ),
        raw_text="Send an email to both recipients.",
    )

    action = result["actions"][0]
    assert action["routing_category"] == "prepare_for_human_review"
    assert action["parameters"]["recipient"] == (
        "first@example.com, second@example.com"
    )


def test_explicit_approval_deferred_send_is_not_mistaken_for_draft_only():
    result = normalize_analysis(
        analysis_for(
            "draft_communication",
            {
                "recipient": "person@example.com",
                "subject": "Agent Integration Test",
                "body": "Please review this message.",
            },
        ),
        raw_text=EMAIL_REQUEST,
    )

    action = result["actions"][0]
    assert action["tool_to_use"] == "send_email"
    assert action["routing_category"] == "prepare_for_human_review"


def test_draft_only_request_remains_a_local_draft_and_never_a_send():
    result = normalize_analysis(
        analysis_for(
            "draft_communication",
            {
                "recipient": "person@example.com",
                "subject": "Draft only",
                "body": "Please review this draft.",
            },
        ),
        raw_text="Draft an email to person@example.com and show it to me.",
    )

    action = result["actions"][0]
    assert action["tool_to_use"] == "draft_communication"
    assert action["routing_category"] == "execute_automatically"


def test_explicit_send_request_routes_to_approval_even_without_deferred_phrase():
    result = normalize_analysis(
        analysis_for(
            "draft_communication",
            {
                "recipient": "person@example.com",
                "subject": "Please send",
                "body": "Test message.",
            },
        ),
        raw_text="Send an email to person@example.com with this message.",
    )

    action = result["actions"][0]
    assert action["tool_to_use"] == "send_email"
    assert action["routing_category"] == "prepare_for_human_review"


def test_plain_dont_send_request_stays_a_local_draft():
    result = normalize_analysis(
        analysis_for(
            "send_email",
            {
                "recipient": "person@example.com",
                "subject": "Draft only",
                "body": "Do not send this yet.",
            },
        ),
        raw_text="Prepare a draft email. Do not send it.",
    )

    action = result["actions"][0]
    assert action["tool_to_use"] == "draft_communication"
    assert action["routing_category"] == "execute_automatically"


def test_only_missing_email_details_are_requested():
    result = normalize_analysis(
        analysis_for(
            "draft_communication",
            {"subject": "Agent Integration Test", "body": "Test body"},
            missing=["url", "title", "deadline", "priority", "description", "due_date"],
        ),
        raw_text="Prepare a message and send it after I approve.",
    )

    action = result["actions"][0]
    assert action["tool_to_use"] == "send_email"
    assert action["routing_category"] == "requires_clarification"
    assert action["reason"] == "Enter one or more valid, unique email recipients."
    assert result["missing_information"] == [
        "Enter one or more valid, unique email recipients."
    ]


def test_website_and_task_routes_keep_their_action_specific_fields():
    website = normalize_analysis(
        analysis_for(
            "check_website_status",
            {"url": "https://example.com"},
            category="execute_automatically",
            missing=["recipient", "subject", "body", "url"],
        )
    )
    task = normalize_analysis(
        analysis_for(
            "create_task_record",
            {"title": "Review proposal"},
            category="prepare_for_human_review",
            missing=["url", "recipient", "subject", "body", "title"],
        )
    )

    assert website["actions"][0]["routing_category"] == "execute_automatically"
    assert website["missing_information"] == []
    assert task["actions"][0]["routing_category"] == "prepare_for_human_review"
    assert task["missing_information"] == []


def test_unexecutable_email_is_not_reclassified_for_approval():
    result = normalize_analysis(
        analysis_for(
            "send_email",
            {
                "recipient": "person@example.com",
                "subject": "Status",
                "body": "Ready.",
            },
            category="cannot_execute_with_tools",
        ),
        raw_text="Send an email to person@example.com.",
    )

    assert result["actions"][0]["routing_category"] == "cannot_execute_with_tools"


def test_unexecutable_trello_action_is_not_reclassified_for_approval():
    result = normalize_analysis(
        analysis_for(
            "create_trello_card",
            {
                "title": "Review proposal",
                "description": "Review the incoming proposal.",
            },
            category="cannot_execute_with_tools",
        )
    )

    assert result["actions"][0]["routing_category"] == "cannot_execute_with_tools"
