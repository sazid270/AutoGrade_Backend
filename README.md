# AutoGrade - Backend API Server

AutoGrade is a print-on-demand store that transforms creativity into high-quality apparel, home decor, and accessories. This repository contains the backend API and server implementation for the AutoGrade platform.

## Features

- **Admin API:** Complete management system for banners, advertisements, categories, users, and products
- **User Management:** Profile settings and wishlist management APIs
- **Product API:** Endpoints for browsing, filtering, and searching products
- **Authentication:** Passwordless email OTP and Google sign-in with secure JWT issuance
- **Media Handling:** Optimized image processing and storage for product visuals

## Documentation

- [Installation Guide](docs/INSTALLATION.md)
- [Server & Gunicorn Setup](docs/SERVER.md)
- [Scheduled Tasks (Cronjobs)](docs/CRONJOBS.md)
- [Dependency checker (Deptry)](docs/DEPTRY.md)

## Tech Stack

- [Django](https://www.djangoproject.com/) - Python web framework
- [Django REST Framework](https://www.django-rest-framework.org/) - REST API toolkit
- [PostgreSQL](https://www.postgresql.org/) - Primary database
- [JWT](https://jwt.io/) - Token-based authentication

## Developer Guide

### Branch Management

1. Start by pulling the `develop` branch
2. Add new features and make changes on `develop` branch
3. Once everything is ok merge the code to `main` branch
