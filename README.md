# 🌟 Ultimate Telegram File Store & Share Bot

![Python Version](https://img.shields.io/badge/Python-3.12%2B-blue?logo=python&logoColor=white)
![Telegram Bot](https://img.shields.io/badge/Telegram-Bot%20API-2CA5E0?logo=telegram&logoColor=white)
![MongoDB Atlas](https://img.shields.io/badge/MongoDB-Atlas-47A248?logo=mongodb&logoColor=white)
![Vercel Serverless](https://img.shields.io/badge/Vercel-Serverless-black?logo=vercel&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)

A production-ready, highly scalable, and lightweight **Telegram File Store & Share Bot** built with **Python**, **Flask**, **python-telegram-bot**, **MongoDB Atlas**, and **Vercel Serverless Functions**.

This bot stores Telegram media `file_id`s directly without downloading files to disk, generating instant, highly secure, shareable deep links.

---

## 💎 Bot Information

- **Creator:** [@YorichiiPrime](https://t.me/YorichiiPrime)
- **Federation:** [@YoriFederation](https://t.me/YoriFederation)
- **Owner ID:** `7728424218`

---

## ✨ Core Features

- **📂 Upload Any Telegram Media:** Documents, Videos, Photos, Audio, Voice, Animations, and Stickers.
- **⚡ Zero Disk Footprint:** Reuses Telegram cached `file_id`s for lightning-fast media delivery.
- **🔗 Advanced Share Links:**
  - **🟢 Permanent Links:** Standard public sharing.
  - **1️⃣ One-Time Links:** Automatically disables access after a single successful download.
  - **🔒 Password-Protected Links:** Encrypted access validated against `bcrypt` password hashes.
  - **⏱ Expiring Links:** Timers for `1h`, `6h`, `12h`, `24h`, `7d`, and `30d`. Automatically disabled upon expiration.
- **🔎 Fast Indexed Search:** Instant partial and extension matching across user filenames.
- **📄 Interactive My Files:** Full pagination (5 files per page), detail views, inline rename/delete/export buttons.
- **📤 TXT Link Export:** Generates a complete text document containing every active share link for the user.
- **📊 Comprehensive User Analytics:** Track total uploads, download generation, join date, and storage volume managed.
- **🛡️ Admin Command Dashboard:** Telemetry tracking, system memory estimation, user broadcasting, and user banning/unbanning.

---

## 📁 Project Structure

```text
yorifile-bot/
├── api/
│   └── webhook.py       # WSGI Flask App & Vercel Serverless Entry Point
├── config.py            # Environment Variables & Application Constants
├── database.py          # PyMongo MongoDB Atlas Connection Pooling & CRUD
├── requirements.txt     # Python Dependencies
├── vercel.json          # Vercel Serverless Route Routing & Configuration
└── README.md            # Documentation & Deployment Guide
```

---

## ⚙️ Installation & Local Setup

1. **Clone the repository:**
   ```bash
  
-git clone https://github.com/yourusername/yorifile-bot.git
-cd yorifile-bot
+git clone https://github.com/jyotishmoigogoigogoi-wq/lkjhgfcgvhbnj.git
+cd lkjhgfcgvhbnj
2. **Create and activate a virtual environment:**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure Environment Variables:**
   Create a `.env` file in the root directory:
   ```env
   BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz
   MONGODB_URI=mongodb+srv://dbuser:dbpass@cluster0.abcde.mongodb.net/?retryWrites=true&w=majority
   WEBHOOK_URL=https://your-bot-domain.vercel.app/api/webhook
   ADMIN_IDS=7728424218
   ```

5. **Run Locally:**
   ```bash
   python api/webhook.py
   # Or using Gunicorn
   gunicorn -w 2 -b 0.0.0.0:5000 api.webhook:app
   ```

---

## 🍃 MongoDB Atlas Setup

1. Create a free cluster on [MongoDB Atlas](https://www.mongodb.com/cloud/atlas).
2. Navigate to **Database Access** and create a database user with **Read and Write to any database** privileges.
3. Navigate to **Network Access** and add `0.0.0.0/0` (Allow Access from Anywhere) to permit Vercel serverless IP rotation.
4. Click **Connect -> Connect your application** and copy the connection string into your `MONGODB_URI` environment variable.
5. *Note:* Collections (`users`, `files`, `settings`, `logs`) and performance indexes are created automatically on bot startup!

---

## 🚀 Deployment on Vercel

1. Install the Vercel CLI or connect your GitHub repository to the [Vercel Dashboard](https://vercel.com).
2. Set the Framework Preset to **Other** (Python WSGI is auto-detected via `api/webhook.py` and `vercel.json`).
3. Add the following **Environment Variables** in your Vercel project settings:
   - `BOT_TOKEN`
   - `MONGODB_URI`
   - `WEBHOOK_URL` (Your production Vercel URL ending in `/api/webhook`)
   - `ADMIN_IDS` (`7728424218`)
4. Deploy the application:
   ```bash
   vercel --prod
   ```

---

## 🔗 Webhook Setup

Once deployed to Vercel, register your webhook endpoint with Telegram API by opening your browser or running `curl`:

```bash
curl -F "url=https://your-bot-domain.vercel.app/api/webhook" \
     "https://api.telegram.org/bot<YOUR_BOT_TOKEN>/setWebhook"
```

To verify webhook status:
```bash
curl "https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getWebhookInfo"
```

---

## 📖 Usage & Commands

### 👤 User Commands

| Command | Description |
| :--- | :--- |
| `/start` | Display main menu or access a shared file link (`?start=FILEKEY`) |
| `/help` | View comprehensive uploading and command guide |
| `/myfiles` | Open interactive file manager with pagination and controls |
| `/search <query>` | Fast case-insensitive indexed search by filename or extension |
| `/mystats` | View personal upload/download metrics and storage volume |
| `/rename <key> <name>` | Rename a stored file record |
| `/delete <key>` | Delete a file from storage permanently |
| `/export` | Receive a `.txt` deliverable listing every active share link |
| `/onetime <key>` | Toggle single-download restriction mode |
| `/password <key> <pass>` | Encrypt link with a password (use `none` to remove) |
| `/expire <key> <time>` | Set expiration timer (`1h`, `6h`, `12h`, `24h`, `7d`, `30d`, `none`) |

### 🛡️ Admin Panel Commands (Admin Only)

| Command | Description |
| :--- | :--- |
| `/admin` | Open interactive Admin Dashboard menu |
| `/users` | Display total registered user count and recent member list |
| `/stats` | View system telemetry, total DB volume, and memory footprint |
| `/broadcast <text>` | Deliver a batch broadcast message to all registered users |
| `/ban <user_id>` | Revoke bot access for a specific user ID |
| `/unban <user_id>` | Restore bot access for a specific user ID |
| `/logs` | Display recent system activity telemetry logs |

---

## 📄 License

This project is licensed under the **MIT License**. Created by **[@YorichiiPrime](https://t.me/YorichiiPrime)** for **[@YoriFederation](https://t.me/YoriFederation)**.
