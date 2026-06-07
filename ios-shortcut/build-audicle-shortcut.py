"""Build the Audicle iOS Shortcut as a signed .shortcut file.

Share an article link from any app -> the shortcut logs in (password mode),
submits the URL to /api/v1/submit, and notifies that it was queued.
Fire-and-forget: it does not poll for processing completion.

After importing the signed shortcut, edit the two Text actions at the top to set
your server URL and admin password (see docs/ios-shortcut.md).
"""
import os
import plistlib
import subprocess
import sys

# Group UUIDs for control flow blocks
GID_IF_URL = "A0000002-0000-0000-0000-000000000001"
GID_IF_NO_CSRF = "A0000002-0000-0000-0000-000000000002"
GID_IF_QUEUED = "A0000002-0000-0000-0000-000000000003"


def var_ref(name):
    """Variable reference for use as action parameter values (WFInput, etc.)."""
    return {
        "Value": {"VariableName": name, "Type": "Variable"},
        "WFSerializationType": "WFTextTokenAttachment",
    }


def var_token(name):
    """Variable reference for embedding inside WFTextTokenString attachmentsByRange."""
    return {"VariableName": name, "Type": "Variable"}


def shortcut_input():
    """Reference the Shortcut Input (Share Sheet)."""
    return {
        "Value": {"Type": "ExtensionInput"},
        "WFSerializationType": "WFTextTokenAttachment",
    }


def text_with_vars(parts):
    """Build a WFTextTokenString from a list of (string, var_name_or_None) tuples.

    Uses var_token (raw dict) not var_ref (wrapped) for text embeddings.
    """
    full_string = ""
    attachments = {}
    for text_part, var_name in parts:
        if var_name:
            pos = len(full_string) + len(text_part)
            full_string += text_part + "\ufffc"
            attachments[f"{{{pos}, 1}}"] = var_token(var_name)
        else:
            full_string += text_part
    return {
        "Value": {"attachmentsByRange": attachments, "string": full_string},
        "WFSerializationType": "WFTextTokenString",
    }


def plain_text(s):
    """A literal WFTextTokenString with no embedded variables."""
    return {
        "Value": {"string": s, "attachmentsByRange": {}},
        "WFSerializationType": "WFTextTokenString",
    }


def act(identifier, params=None):
    """Build a shortcut action dict."""
    return {
        "WFWorkflowActionIdentifier": identifier,
        "WFWorkflowActionParameters": params or {},
    }


actions = []

# ============================================================
# SETUP: article URL from Share Sheet + editable config
# ============================================================

# URL directly from Shortcut Input (must use shortcut_input() directly).
actions.append(act("is.workflow.actions.detect.link", {"WFInput": shortcut_input()}))
actions.append(act("is.workflow.actions.setvariable", {"WFVariableName": "urls"}))

actions.append(act("is.workflow.actions.getitemfromlist", {
    "WFInput": var_ref("urls"),
    "WFItemSpecifier": "First Item",
}))
actions.append(act("is.workflow.actions.setvariable", {"WFVariableName": "articleUrl"}))

# Editable config: edit these two Text actions in the Shortcuts app after import.
actions.append(act("is.workflow.actions.gettext", {
    "WFTextActionText": plain_text("https://YOUR-AUDICLE-HERE.example.com"),
}))
actions.append(act("is.workflow.actions.setvariable", {"WFVariableName": "server"}))

actions.append(act("is.workflow.actions.gettext", {
    "WFTextActionText": plain_text("YOUR-PASSWORD-HERE"),
}))
actions.append(act("is.workflow.actions.setvariable", {"WFVariableName": "password"}))

# ============================================================
# IF urls has any value -> do the work
# ============================================================
actions.append(act("is.workflow.actions.conditional", {
    "GroupingIdentifier": GID_IF_URL,
    "WFControlFlowMode": 0,
    "WFCondition": 100,  # has any value
    "WFInput": {"Type": "Variable", "Variable": var_ref("urls")},
}))

# ---- Login: POST {server}/api/v1/auth/login {"password": password} ----
# The URL field carries the server variable directly so it is explicitly wired.
actions.append(act("is.workflow.actions.downloadurl", {
    "WFURL": text_with_vars([("", "server"), ("/api/v1/auth/login", None)]),
    "WFHTTPMethod": "POST",
    "WFHTTPBodyType": "JSON",
    "WFJSONValues": {"Value": {"WFDictionaryFieldValueItems": [
        {"WFItemType": 0,
         "WFKey": {"Value": {"string": "password"}, "WFSerializationType": "WFTextTokenString"},
         "WFValue": text_with_vars([("", "password")]),
        },
    ]}, "WFSerializationType": "WFDictionaryFieldValue"},
}))
actions.append(act("is.workflow.actions.setvariable", {"WFVariableName": "loginResponse"}))

actions.append(act("is.workflow.actions.detect.dictionary", {"WFInput": var_ref("loginResponse")}))
actions.append(act("is.workflow.actions.setvariable", {"WFVariableName": "loginDict"}))

actions.append(act("is.workflow.actions.getvalueforkey", {
    "WFInput": var_ref("loginDict"),
    "WFDictionaryKey": "csrf_token",
}))
actions.append(act("is.workflow.actions.setvariable", {"WFVariableName": "csrf"}))

# If login failed (no csrf_token) -> notify and stop.
actions.append(act("is.workflow.actions.conditional", {
    "GroupingIdentifier": GID_IF_NO_CSRF,
    "WFControlFlowMode": 0,
    "WFCondition": 101,  # does NOT have any value
    "WFInput": {"Type": "Variable", "Variable": var_ref("csrf")},
}))
actions.append(act("is.workflow.actions.notification", {
    "WFNotificationActionTitle": "Audicle",
    "WFNotificationActionBody": plain_text("Login failed -- check the password in the shortcut."),
}))
actions.append(act("is.workflow.actions.exit", {}))
actions.append(act("is.workflow.actions.conditional", {
    "GroupingIdentifier": GID_IF_NO_CSRF,
    "WFControlFlowMode": 2,
}))

# ---- Submit: POST {server}/api/v1/submit {"url": articleUrl} + X-CSRF-Token ----
actions.append(act("is.workflow.actions.downloadurl", {
    "WFURL": text_with_vars([("", "server"), ("/api/v1/submit", None)]),
    "WFHTTPMethod": "POST",
    "WFHTTPBodyType": "JSON",
    "WFJSONValues": {"Value": {"WFDictionaryFieldValueItems": [
        {"WFItemType": 0,
         "WFKey": {"Value": {"string": "url"}, "WFSerializationType": "WFTextTokenString"},
         "WFValue": text_with_vars([("", "articleUrl")]),
        },
    ]}, "WFSerializationType": "WFDictionaryFieldValue"},
    "WFHTTPHeaders": {"Value": {"WFDictionaryFieldValueItems": [
        {"WFItemType": 0,
         "WFKey": {"Value": {"string": "X-CSRF-Token"}, "WFSerializationType": "WFTextTokenString"},
         "WFValue": text_with_vars([("", "csrf")]),
        },
    ]}, "WFSerializationType": "WFDictionaryFieldValue"},
}))
actions.append(act("is.workflow.actions.setvariable", {"WFVariableName": "submitResponse"}))

actions.append(act("is.workflow.actions.detect.dictionary", {"WFInput": var_ref("submitResponse")}))
actions.append(act("is.workflow.actions.setvariable", {"WFVariableName": "submitDict"}))

actions.append(act("is.workflow.actions.getvalueforkey", {
    "WFInput": var_ref("submitDict"),
    "WFDictionaryKey": "job_id",
}))
actions.append(act("is.workflow.actions.setvariable", {"WFVariableName": "jobid"}))

# If job_id present -> queued, else surface the error detail.
actions.append(act("is.workflow.actions.conditional", {
    "GroupingIdentifier": GID_IF_QUEUED,
    "WFControlFlowMode": 0,
    "WFCondition": 100,  # has any value
    "WFInput": {"Type": "Variable", "Variable": var_ref("jobid")},
}))
actions.append(act("is.workflow.actions.notification", {
    "WFNotificationActionTitle": "Audicle",
    "WFNotificationActionBody": plain_text("Queued for processing."),
}))
# Otherwise
actions.append(act("is.workflow.actions.conditional", {
    "GroupingIdentifier": GID_IF_QUEUED,
    "WFControlFlowMode": 1,
}))
actions.append(act("is.workflow.actions.getvalueforkey", {
    "WFInput": var_ref("submitDict"),
    "WFDictionaryKey": "detail",
}))
actions.append(act("is.workflow.actions.setvariable", {"WFVariableName": "detail"}))
actions.append(act("is.workflow.actions.notification", {
    "WFNotificationActionTitle": "Audicle",
    "WFNotificationActionBody": text_with_vars([("Submit failed: ", None), ("", "detail")]),
}))
# End If queued
actions.append(act("is.workflow.actions.conditional", {
    "GroupingIdentifier": GID_IF_QUEUED,
    "WFControlFlowMode": 2,
}))

# End If urls has value
actions.append(act("is.workflow.actions.conditional", {
    "GroupingIdentifier": GID_IF_URL,
    "WFControlFlowMode": 2,
}))

# ============================================================
# Build plist and sign
# ============================================================
shortcut = {
    "WFWorkflowMinimumClientVersion": 900,
    "WFWorkflowMinimumClientVersionString": "900",
    "WFWorkflowHasShortcutInputVariables": True,
    "WFWorkflowNoInputBehavior": {
        "Name": "WFWorkflowNoInputBehaviorGetClipboard",
        "Parameters": {},
    },
    "WFWorkflowIcon": {
        "WFWorkflowIconStartColor": 463140863,
        "WFWorkflowIconGlyphNumber": 59651,  # headphones
    },
    "WFWorkflowTypes": ["ActionExtension"],
    "WFWorkflowInputContentItemClasses": [
        "WFArticleContentItem",
        "WFRichTextContentItem",
        "WFSafariWebPageContentItem",
        "WFStringContentItem",
        "WFURLContentItem",
    ],
    "WFWorkflowActions": actions,
}

unsigned = "/tmp/audicle-unsigned.shortcut"
signed = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Audicle.shortcut")

with open(unsigned, "wb") as f:
    plistlib.dump(shortcut, f, fmt=plistlib.FMT_BINARY)

result = subprocess.run(
    ["shortcuts", "sign", "--mode", "anyone", "--input", unsigned, "--output", signed],
    capture_output=True, text=True,
)
if result.returncode == 0:
    print(f"Built: {signed}")
else:
    print(f"Failed: {result.stderr}")
    sys.exit(1)
