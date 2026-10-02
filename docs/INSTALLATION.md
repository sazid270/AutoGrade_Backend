# Installation Guide

This guide covers setting up AutoGrade Backend for local Windows development
and the Linux production server.

## Local Installation (Windows)

1. Clone this repository:

```sh
git clone https://github.com/sazid270/AutoGrade_Backend.git
```

2. Change to the project directory:

```sh
cd AutoGrade_Backend
```

3. Run the Windows setup script from the project root. This creates and
   configures `.venv`:

   ```sh
   scripts\setup.bat
   ```

4. Activate the environment when opening a new terminal:

```sh
.\.venv\Scripts\activate
```

5. Environment Setup

- See `.env.example` for required variables and create `.env` file.

6. Apply migrations

```sh
python manage.py makemigrations
python manage.py migrate
```

7. Start the development server

```sh
python manage.py runserver
```

## Server Installation (Linux)

1. Clone this repository:

```sh
git clone https://github.com/sazid270/AutoGrade_Backend.git
```

2. Change to the project directory:

```sh
cd AutoGrade_Backend
```

3. Run the Linux setup script from the project root. This creates and
   configures `.venv`:

   ```sh
   bash scripts/setup.sh
   ```

4. Environment Setup

- See `.env.example` for required variables and create `.env` file.

5. Start the Server

```sh
bash scripts/server.sh --start
```

## Deactivate the environment

```sh
deactivate
```
