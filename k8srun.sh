#!/usr/bin/env bash

minikube start --mount-string="/Users/Omkar/opt/hass:/opt/hass" --mount
minikube addons enable ingress

kubectl apply -f k8s/ingress
kubectl apply -f k8s/homeassistant
