FROM python:3.9-slim

ARG PORT
ARG HOST

WORKDIR /usr/src/app

COPY requirements.txt .
RUN pip install -r requirements.txt

# Install SSH client
RUN apt-get update && apt-get install -y openssh-client

COPY . .

# Add the private key and set permissions
RUN mkdir -p /root/.ssh

COPY id_ed25519 /root/.ssh/id_ed25519
RUN chmod 600 /root/.ssh/id_ed25519

# Add the host's key to known hosts to avoid the first-time connection prompt
RUN ssh-keyscan -p $PORT $HOST >> /root/.ssh/known_hosts

CMD ["python", "app.py"]
