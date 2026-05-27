---
name: Bug report
description: Report a problem with the bridge
title: "bug: "
labels: ["needs-triage"]
body:
  - type: textarea
    id: description
    attributes:
      label: Bug description
      description: What happened?
    validations:
      required: true
  - type: textarea
    id: reproduce
    attributes:
      label: Steps to reproduce
      description: Include commands or sanitized VK/Yandex/Hermes events if possible.
      placeholder: |
        1. ...
        2. ...
        3. ...
    validations:
      required: true
  - type: textarea
    id: expected
    attributes:
      label: Expected behavior
  - type: textarea
    id: actual
    attributes:
      label: Actual behavior
  - type: textarea
    id: environment
    attributes:
      label: Environment
      description: Do not paste secrets. Redact tokens, keys, queue credentials, API keys, chat/user IDs if needed.
      placeholder: |
        - OS:
        - Python:
        - Node.js:
        - Hermes Agent version:
        - VK API version:
        - Deployment mode: Yandex Callback + Queue / Long Poll / local
  - type: textarea
    id: logs
    attributes:
      label: Sanitized logs
      description: Paste only sanitized logs. Never include .env contents or tokens.
