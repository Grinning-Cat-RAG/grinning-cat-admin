# Grinning Cat Admin UI

A modern web-based administration interface for the [Grinning Cat Core](https://github.com/matteocacciola/grinning-cat-core) framework. This admin UI provides a user-friendly interface to manage, configure, and monitor your Grinning Cat AI assistant instances.

## Features

- 🎯 **Dashboard Overview** - Real-time monitoring of Grinning Cat instances
- 🔧 **Configuration Management** - Easy-to-use interface for system settings
- 🧩 **Plugin Management** - Install, configure, and manage plugins
- 👥 **User Management** - Handle user accounts and permissions
- 📊 **Analytics & Logs** - Monitor conversations and system performance
- 🔐 **Authentication & Security** - Secure access control and user management
- 🎨 **Modern UI** - Clean, responsive interface built with modern web technologies

## Prerequisites

- Python 3.11 or higher
- pip and venv for dependency management
- Running instance of [Grinning Cat Core](https://github.com/matteocacciola/grinning-cat-core)

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/matteocacciola/grinning-cat-admin.git
cd grinning-cat-admin
```

### 2. Create and activate virtual environment

```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

### 3. Install dependencies

```bash
make install
```

### 4. Set up environment variables

Copy the example environment file and configure it:

```bash
cp .env.example .env
```

Edit `.env` with your settings.

### 5. Run the application

```bash
make run
```

The admin interface will be available at `http://localhost:8501`

## Configuration

### Connecting to Grinning Cat Core

The admin UI connects to your Grinning Cat Core instance via REST API. Configure the connection in your `.env` file:

```env
GRINNING_CAT_API_HOST=your-grinning-cat-instance
GRINNING_CAT_API_PORT=1865
GRINNING_CAT_API_SECURE_CONNECTION=false
```

### Authentication

The admin interface supports two ways of authenticating against Grinning Cat Core:

- **Credentials**: username and password are exchanged for a JWT (plus a
  refresh token) on the Core instance. Both are kept in the browser's
  localStorage, and the permissions of the logged-in user decide which sections
  are reachable. The access token is renewed automatically shortly before its
  `exp` claim, and after a page reload once it has expired, using the
  single-use refresh token (which is rotated at every renewal). When the
  refresh token is refused or expires, the user is sent back to the login form.
  With several tabs open in the same browser, the tokens stored by another tab
  are read before every renewal, so a tab never presents an already-rotated
  refresh token (which the Core would treat as theft). Logging out from one tab
  logs out the others too, immediately (the Core only revokes the refresh
  token, so the access token of the other tabs would otherwise keep working
  until its `exp`).
  **Logout** revokes the session on the Core instance, not only in the browser.
  The login form reports wrong credentials and rate limiting (`429`) with a
  readable message.
- **API key**: set `GRINNING_CAT_API_KEY` to the `CAT_API_KEY` of your Core
  instance. The login form is skipped and every call is made with that key, so
  the whole UI is available to anyone who can reach it. Leave it unset unless
  the UI is only exposed to trusted users.

## Contributing

We welcome contributions! Please see our [Contributing Guide](CONTRIBUTING.md) for details.

### Development Workflow

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/amazing-feature`
3. Make your changes and add tests, then run them with `make test`
4. Commit your changes: `git commit -m 'Add amazing feature'`
5. Push to the branch: `git push origin feature/amazing-feature`
6. Open a Pull Request

## License

This project is licensed under [GPL3](LICENSE).

## Support

- 🐛 [Issue Tracker](https://github.com/matteocacciola/grinning-cat-admin/issues)
- 📧 [Email Support](mailto:matteo.cacciola@gmail.com)

## Acknowledgments

- [Grinning Cat Core](https://github.com/matteocacciola/grinning-cat-core) - The AI framework this admin interface manages
- [FastAPI](https://fastapi.tiangolo.com/) - Modern web framework for building APIs

---

**Made with ❤️ for the Grinning Cat community**