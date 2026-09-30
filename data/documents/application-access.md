---
document_id: DOC-ACC-001
title: Application Access Policy
version: "4.1"
effective_date: 2026-02-01
department: security
classification: internal
source: Identity & Access Management / POL-IAM-01
---
# Application Access Policy

## How to request access
All application access is requested through AegisDesk. Access is granted to the requesting employee only; you cannot request access on behalf of someone else. Standard requests are completed within 2 business days.

## Access by application
- Jira: every employee; granted automatically.
- GitHub: engineering employees; see the GitHub Access Policy.
- Salesforce: sales employees; granted automatically for the sales department.
- FinanceERP: finance employees only, and each request needs approval from the employee's manager.
- AnalyticsHub: selected business teams; the data owner must approve.
- ProductionDB: highly restricted; see the Production Database Access Policy.
- HRAdmin: HR administrators only; requests from anyone else are rejected.

## Access reviews
Managers review their team's access every quarter. Access that has not been used for 90 days is revoked automatically. Contractors' access expires on their contract end date.

## Separation of duties
Nobody may approve their own access request. An employee cannot hold both the FinanceERP "AP Clerk" and "Controller" roles.
