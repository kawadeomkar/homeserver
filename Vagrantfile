# -*- mode: ruby -*-
# vi: set ft=ruby :


Vagrant.configure("2") do |config|

  config.vm.box = "generic/ubuntu2204"

  config.ssh.insert_key = false

  config.vbguest.auto_update = true

  config.vm.network "private_network", ip: "10.0.2.15"
  config.vm.network 'forwarded_port', host: 8016, guest: 30016, host_ip: '127.0.0.1'

  config.vm.provider "virtualbox" do |v|
    v.memory = 8192
    v.cpus = 4
  end

  config.vm.provision "ansible" do |ansible|
    ansible.verbose = true
    ansible.compatibility_mode = "2.0"
    ansible.playbook = "playbook.yaml"
    ansible.ask_vault_pass = true
    ansible.ask_become_pass = true
    ansible.extra_vars = "@vm-vault.enc"
    ansible.raw_arguments = ["-e", "@vm-vault.enc"]
  end
end
