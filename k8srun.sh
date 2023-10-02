#!/usr/bin/env bash

minikube start --kubernetes-version v1.26.3 --mount-string="/Users/Omkar/opt/hass:/opt/hass" --mount
minikube addons enable ingress
minikube addons enable ingress-dns
minikube addons enable registry
minikube addons enable dashboard
minikube addons enable metrics-server

minikube kubectl -- apply -f k8s/ingress
minikube kubectl -- apply -f k8s/homeassistant
