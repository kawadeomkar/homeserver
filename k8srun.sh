#!/usr/bin/env bash

minikube start --mount-string="/Users/Omkar/opt/hass:/opt/hass" --mount
minikube addons enable ingress
minikube addons enable ingress-dns
minikube addons enable storage
minikube addons enable registry
minikube addons enable dashboard
minikube addons enable metrics-server

kubectl apply -f k8s/ingress
kubectl apply -f k8s/homeassistant

minikube dashboard