---
document_id: DOC-PDB-001
title: Production Database Access Policy
version: "1.4"
effective_date: 2026-05-15
department: it
classification: restricted
allowed_roles: [it_admin]
source: Platform Engineering / POL-PLT-09
---
# Production Database Access Policy

## Access path
Production databases are reachable only through the bastion host db-bastion.northstar.internal. Direct connections are blocked by the network.

## Read and write access
Access is read-only by default and granted just in time for a maximum of 4 hours. Write access requires a change ticket approved by the Change Advisory Board (CAB).

## Monitoring
All queries are logged and reviewed weekly by Platform Engineering. Exporting production customer data to a laptop is forbidden.

## Emergencies
In an emergency, write access can be granted through the break-glass procedure, which requires approval from the IT Service Manager and a post-incident review.
