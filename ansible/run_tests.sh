#!/bin/bash

set -euox pipefail


# lint yaml
yamllint .
ansible-lint playbook.yml

# check syntax
ansible-playbook playbook.yml --check-syntax
