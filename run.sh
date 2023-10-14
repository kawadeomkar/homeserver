#!/usr/bin/env bash

ansible-playbook -K -i inventory setup-playbook.yml

ansible-playbook -K -i inventory k8s-playbook.yml
