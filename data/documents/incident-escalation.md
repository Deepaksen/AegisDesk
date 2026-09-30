---
document_id: DOC-INC-001
title: Incident Escalation Guide
version: "3.0"
effective_date: 2026-06-01
department: it
classification: confidential
allowed_roles: [it_admin, manager]
source: IT Operations / RUN-OPS-03
---
# Incident Escalation Guide

## Priority levels
- P1: a whole department cannot work, or an active security incident. Response within 15 minutes, 24/7.
- P2: a business-critical application is degraded for many users. Response within 2 hours during business hours.
- P3: a single user is affected. Response within 1 business day.

## Escalation path
The on-call engineer is paged through PagerDuty for every P1. If a P1 is not mitigated within 30 minutes, escalate to the IT Service Manager, Kenji Watanabe. Security incidents are escalated to the Security on-call at the same time.

## Communication
For P1 and P2 incidents, post an update in the #it-status channel every 30 minutes until resolved. A post-incident review is required for every P1 within 5 business days.
