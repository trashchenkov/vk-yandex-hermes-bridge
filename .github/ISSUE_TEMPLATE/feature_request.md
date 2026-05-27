---
name: Feature request
description: Suggest an improvement for the VK/Yandex/Hermes bridge
title: "feat: "
labels: ["needs-triage"]
body:
  - type: textarea
    id: feature
    attributes:
      label: Feature description
      description: What should the bridge do?
    validations:
      required: true
  - type: textarea
    id: motivation
    attributes:
      label: Motivation
      description: Why is this useful? Who benefits?
    validations:
      required: true
  - type: textarea
    id: solution
    attributes:
      label: Proposed solution
      description: Describe a possible implementation or behavior.
  - type: dropdown
    id: area
    attributes:
      label: Area
      options:
        - security/access-control
        - VK API
        - Yandex Cloud
        - Hermes integration
        - public FAQ/RAG mode
        - observability
        - developer experience
        - documentation
  - type: textarea
    id: alternatives
    attributes:
      label: Alternatives considered
      description: Optional alternatives or trade-offs.
