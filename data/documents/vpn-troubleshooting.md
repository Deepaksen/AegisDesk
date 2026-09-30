---
document_id: DOC-VPN-001
title: VPN Troubleshooting Guide
version: "2.4"
effective_date: 2026-05-02
department: it
classification: internal
source: IT Networking / KB-0042
---
# VPN Troubleshooting Guide

## About the Northstar VPN
Northstar uses the GlobalProtect client, version 6.2 or later. The portal address is vpn.northstar.example. The VPN is required for all internal systems when you are not in a Northstar office. Idle sessions are disconnected after 8 hours.

## Configuring the VPN on macOS
1. Install GlobalProtect from the Self Service app (search for "GlobalProtect").
2. Open GlobalProtect from the menu bar and enter the portal vpn.northstar.example.
3. Sign in with your Northstar SSO account and approve the Okta Verify push.
4. When macOS asks to allow the system extension, open System Settings > Privacy & Security and click Allow.

## Configuring the VPN on Windows
GlobalProtect is pre-installed on Windows laptops. Open it from the system tray, enter vpn.northstar.example and sign in with SSO.

## The VPN keeps disconnecting
Frequent drops (for example every 10 minutes) are usually caused by Wi-Fi power saving or an outdated client.
1. Check that GlobalProtect is version 6.2 or later; update it from Self Service if not.
2. On macOS, disable "Low Data Mode" for your Wi-Fi network. On Windows, disable power saving for the wireless adapter in Device Manager.
3. Switch to a wired connection or a different network to rule out the local router.
4. If you use a home router with MTU below 1400, ask IT for the low-MTU gateway profile.

## Error GP-512
Error GP-512 means your device certificate has expired. Reinstall the "Northstar Device Certificate" from Self Service and restart the laptop.

## When to raise a ticket
If the VPN still fails after these steps, create a ticket in the VPN category. Include the GlobalProtect version, the time of the last disconnect, and the troubleshooting steps you already tried. Collect logs via GlobalProtect > Settings > Troubleshooting > Collect Logs.
