# Applet Generation Prompts
This file documents the prompts used to generate interactive applets.

## Telegram Architecture: Polling vs Webhooks - telegram-arch.html
Page Context:
Page Title: Telegram Architecture: Polling vs Webhooks
Content Goals: Describe what the Telegram API is and how it communicates with external applications., Compare and contrast short polling, long polling, and webhooks., Identify long polling as the standard approach for local/development Telegram bots.
Generated Page Text:
# Connecting to the Telegram Bot API

Before your multi-agent pipeline can analyze photos or narrate nature documentaries, it needs an ear on the ground. It has to receive incoming chat messages and dispatch outbound replies.

The **Telegram Bot API** acts as the central exchange. It exposes an HTTPS-based interface that handles encrypted user messages on Telegram's infrastructure, then delivers those events to your custom application.To build a bot, you do not connect directly to end users. Telegram acts as an intermediary buffer. When a user sends a photo or text command, Telegram holds that update until your bot picks it up—or until Telegram pushes it to your server.

How your bot receives these updates comes down to two primary networking paradigms: **Polling** and **Webhooks**.## Polling vs. Webhooks

When listening for new messages, your bot can either ask Telegram repeatedly for news, or ask Telegram to ring its doorbell whenever someone types.

### 1. Short Polling
Your server sends a GET request to Telegram: *"Any new updates?"* Telegram replies immediately: *"No."* A split second later, your server asks again. This wastes substantial bandwidth, burns CPU cycles, and risks hitting rate limits.

### 2. Long Polling
Your server opens a single HTTP request and asks Telegram for updates, but specifies a timeout (for example, 30 seconds). Telegram holds that connection open. If a user sends a message during that window, Telegram immediately streams the response over the existing connection and closes it. Your server processes the message and instantly opens a new long poll request.### 3. Webhooks
Instead of your server calling Telegram, Telegram calls your server. Whenever an update occurs, Telegram sends an HTTP POST payload directly to a publicly accessible URL you have registered.## Comparing the Approaches

| Feature | Long Polling | Webhooks |
| :--- | :--- | :--- |
| **Initiator** | Your server initiates outbound requests to Telegram | Telegram initiates inbound requests to your server |
| **Network Requirements** | Works behind NAT, firewalls, and on `localhost` without public IP | Requires a public static IP or domain name, plus a valid SSL/TLS certificate |
| **Latency** | Near zero (real-time return on open socket) | Real-time immediate push |
| **Best For** | Local development, hackathons, and single-instance bots | High-throughput distributed production clusters |

Experiment with the interactive diagram below to see how network packets traverse the wire in both models.## Why We Use Long Polling in This Project

During development, your Python code runs locally on your workstation. Setting up a webhook on `localhost` requires reverse-proxy tunneling tools like ngrok or configuring public DNS records and self-signed certificates.

Long polling requires none of that overhead. Your local script reaches **outbound** to Telegram's secure servers over HTTPS (specifically calling the `getUpdates` endpoint). Firewalls and home routers allow standard outbound requests automatically, meaning your bot functions instantly without exposing any ports to the open internet.

Create a self-contained HTML simulation that implements the following specification.

Description: Interactive diagram showing Polling vs Webhooks
Specification: A split-screen interactive diagram. Left side: 'Polling' - User clicks a button, a server icon repeatedly asks a Telegram server icon 'Any messages?'. The Telegram server replies 'No' several times, then finally 'Yes' and sends a packet. Right side: 'Webhook' - Telegram server icon proactively pushes a packet directly to the server icon the moment a message arrives. Smooth CSS animations for the packets.

The output MUST be a single HTML file with internal <style> and <script> tags. Avoid external dependencies. Ensure it is responsive and uses vanilla CSS/JS. Follow the specification exactly.

IMPORTANT: The page already covers the core concepts (see page text above). Your applet should complement the page — provide an interactive exercise, visual demo, or exploratory tool. Do NOT re-teach material already on the page.
