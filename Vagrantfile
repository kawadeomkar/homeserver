# -*- mode: ruby -*-
# vi: set ft=ruby :


Vagrant.configure("2") do |config|

  config.vm.box = "generic/ubuntu2204"
  config.vm.network "private_network", ip: "10.0.2.15"
  config.vm.network :forwarded_port, host: 8000, guest: 8001

  config.vm.provider "virtualbox" do |v|
    v.memory = 4096
    v.cpus = 2
  end

  config.vm.provision "ansible" do |ansible|
    ansible.playbook = "playbook.yml"
  end
end
