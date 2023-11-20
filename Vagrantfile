# -*- mode: ruby -*-
# vi: set ft=ruby :


Vagrant.configure("2") do |config|

  config.vm.box = "generic/ubuntu2204"
  config.ssh.insert_key = false

  config.vbguest.auto_update = true

  config.vm.network "private_network", ip: "10.0.2.15"
  config.vm.network 'forwarded_port', host: 8000, guest: 8001, host_ip: '127.0.0.1'
  #config.vm.network 'forwarded_port', host: 80,   guest: 80,   host_ip: '127.0.0.1'
  #config.vm.network 'forwarded_port', host: 443, guest:  443,  host_ip: '127.0.0.1'
  

  config.vm.provider "virtualbox" do |v|
    v.memory = 4096
    v.cpus = 2
  end

  config.vm.provision "ansible" do |ansible|
    ansible.compatibility_mode = "2.0"
    ansible.playbook = "playbook.yaml"
  end
end
