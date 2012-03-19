#!/usr/bin/python2.6
#
# The Qubes OS Project, http://www.qubes-os.org
#
# Copyright (C) 2012  Agnieszka Kostrzewa <agnieszka.kostrzewa@gmail.com>
# Copyright (C) 2012  Marek Marczykowski <marmarek@mimuw.edu.pl>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
#
#

import sys
import os
from PyQt4.QtCore import *
from PyQt4.QtGui import *

from qubes.qubes import QubesVmCollection
from qubes.qubes import QubesVmLabels
from qubes.qubes import QubesException
from qubes.qubes import qubes_appmenu_create_cmd
from qubes.qubes import qubes_appmenu_remove_cmd
from qubes.qubes import QubesDaemonPidfile
from qubes.qubes import QubesHost
from qubes.qubes import qrexec_client_path
from qubes.qubes import qubes_kernels_base_dir

import qubesmanager.resources_rc

from pyinotify import WatchManager, Notifier, ThreadedNotifier, EventsCodes, ProcessEvent

import subprocess
import time
import threading
from operator import itemgetter
from copy import copy

from ui_settingsdlg import *
from multiselectwidget import *
from appmenu_select import *
from firewall import *


class VMSettingsWindow(Ui_SettingsDialog, QDialog):
    tabs_indices = {"basic": 0,
                    "advanced": 1,
                    "firewall": 2,
                    "devices": 3,
                    "applications": 4,
                    "services": 5,}

    def __init__(self, vm, app, qvm_collection, init_page="basic", parent=None):
        super(VMSettingsWindow, self).__init__(parent)

        self.app = app
        self.qvm_collection = qvm_collection
        self.vm = vm
        if self.vm.template:
            self.source_vm = self.vm.template
        else:
            self.source_vm = self.vm
 
        self.setupUi(self)
        if init_page in self.tabs_indices:
            idx = self.tabs_indices[init_page]
            assert (idx in range(self.tabWidget.count()))
            self.tabWidget.setCurrentIndex(idx)

        self.connect(self.buttonBox, SIGNAL("accepted()"), self.save_and_apply)
        self.connect(self.buttonBox, SIGNAL("rejected()"), self.reject)

        self.tabWidget.currentChanged.connect(self.current_tab_changed)

        self.tabWidget.setTabEnabled(self.tabs_indices["applications"], not vm.is_netvm())
        self.tabWidget.setTabEnabled(self.tabs_indices["firewall"], vm.is_networked() and not (vm.is_netvm() and not vm.is_proxyvm()))

        ###### basic tab
        self.__init_basic_tab__()

        ###### advanced tab
        self.__init_advanced_tab__()
        self.include_in_balancing.stateChanged.connect(self.include_in_balancing_state_changed)

        ###### firewall tab
        if self.tabWidget.isTabEnabled(self.tabs_indices["firewall"]):

            model = QubesFirewallRulesModel()
            model.set_vm(vm)
            self.set_fw_model(model)

            self.newRuleButton.clicked.connect(self.new_rule_button_pressed)
            self.editRuleButton.clicked.connect(self.edit_rule_button_pressed)
            self.deleteRuleButton.clicked.connect(self.delete_rule_button_pressed)

        ####### devices tab
        self.__init_devices_tab__()
        self.connect(self.dev_list, SIGNAL("selected_changed()"), self.devices_selection_changed)
 
        ####### services tab
        self.__init_services_tab__()
        self.add_srv_button.clicked.connect(self.__add_service__)
        self.remove_srv_button.clicked.connect(self.__remove_service__)

        ####### apps tab
        if self.tabWidget.isTabEnabled(self.tabs_indices["applications"]):
            self.app_list = MultiSelectWidget(self)
            self.apps_layout.addWidget(self.app_list)
            self.AppListManager = AppmenuSelectManager(self.vm, self.app_list)

    def reject(self):
        self.done(0)

    #needed not to close the dialog before applying changes
    def accept(self):
        pass

    def save_and_apply(self):
        thread_monitor = ThreadMonitor()
        thread = threading.Thread (target=self.__save_changes__, args=(thread_monitor,))
        thread.daemon = True
        thread.start()
        
        progress = QProgressDialog ("Applying settings to <b>{0}</b>...".format(self.vm.name), "", 0, 0)
        progress.setCancelButton(None)
        progress.setModal(True)
        progress.show()

        while not thread_monitor.is_finished():
            self.app.processEvents()
            time.sleep (0.1)

        progress.hide()
        
        if not thread_monitor.success:
            QMessageBox.warning (None, "Error while changing settings for {0}!", "ERROR: {1}".format(self.vm.name, thread_monitor.error_msg))

        self.done(0)

    def __save_changes__(self, thread_monitor):

        self.qvm_collection.lock_db_for_writing()
        self.anything_changed = False
        
        ret = self.__apply_basic_tab__()
        self.__apply_advanced_tab__()
        self.__apply_devices_tab__()
        self.__apply_services_tab__()

        if len(ret) > 0 :
            thread_monitor.set_error_msg('\n'.join(ret)) 

        if self.anything_changed == True:
            self.qvm_collection.save()
        self.qvm_collection.unlock_db()

        if self.tabWidget.isTabEnabled(self.tabs_indices["firewall"]):
            self.fw_model.apply_rules(self.policyAllowRadioButton.isChecked(), self.dnsCheckBox.isChecked(), self.icmpCheckBox.isChecked())

        if self.tabWidget.isTabEnabled(self.tabs_indices["applications"]):
            self.AppListManager.save_appmenu_select_changes()

        thread_monitor.set_finished()
 


    def current_tab_changed(self, idx):
        if idx == self.tabs_indices["firewall"]:
            if self.vm.netvm is not None and not self.vm.netvm.is_proxyvm():
                QMessageBox.warning (None, "VM configuration problem!", "The '{0}' AppVM is not network connected to a FirewallVM!<p>".format(self.vm.name) +\
                    "You may edit the '{0}' VM firewall rules, but these will not take any effect until you connect it to a working Firewall VM.".format(self.vm.name))



    ######### basic tab

    def __init_basic_tab__(self):
        self.vmname.setText(self.vm.name)
        
        #self.qvm_collection.lock_db_for_reading()
        #self.qvm_collection.load()
        #self.qvm_collection.unlock_db()
        
        self.label_list = QubesVmLabels.values()
        self.label_list.sort(key=lambda l: l.index)
        self.label_idx = 0
        for (i, label) in enumerate(self.label_list):
            if label == self.vm.label:
                self.label_idx = i
            self.vmlabel.insertItem(i, label.name)
            self.vmlabel.setItemIcon (i, QIcon(label.icon_path))
        self.vmlabel.setCurrentIndex(self.label_idx)

        if not self.vm.is_template() and self.vm.template is not None:
            template_vm_list = [vm for vm in self.qvm_collection.values() if not vm.internal and vm.is_template()]
            self.template_idx = -1

            for (i, vm) in enumerate(template_vm_list):
                text = vm.name
                if vm is self.qvm_collection.get_default_template():
                    text += " (default)"
                if vm.qid == self.vm.template.qid:
                    self.template_idx = i
                    text += " (current)"
                self.template_name.insertItem(i, text)
            self.template_name.setCurrentIndex(self.template_idx)
        else:
            self.template_name.setEnabled(False)
            self.template_idx = -1


        if (not self.vm.is_netvm() or self.vm.is_proxyvm()):
            netvm_list = [vm for vm in self.qvm_collection.values() if not vm.internal and vm.is_netvm()]
            self.netvm_idx = -1

            text = "default ("+self.qvm_collection.get_default_netvm().name+")"
            if self.vm.uses_default_netvm:
                text += " (current)"
                self.netvm_idx = 0
            self.netVM.insertItem(0, text)
    
            for (i, vm) in enumerate(netvm_list):
                text = vm.name
                if self.vm.netvm is not None and vm.qid == self.vm.netvm.qid and not self.vm.uses_default_netvm:
                    self.netvm_idx = i+1
                    text += " (current)"
                self.netVM.insertItem(i+1, text)

            none_text = "none"
            if self.vm.netvm is None:
                none_text += " (current)"
                self.netvm_idx = len(netvm_list)+1
            self.netVM.insertItem(len(netvm_list)+1, none_text)

            self.netVM.setCurrentIndex(self.netvm_idx)
        else:
            self.netVM.setEnabled(False)
            self.netvm_idx = -1

        self.include_in_backups.setChecked(self.vm.include_in_backups)

        #type
        self.type_label.setText(self.vm.type)

        #installed by rpm
        text = "Yes" if self.vm.installed_by_rpm == True else "No"
        self.rpm_label.setText(text)

        #networking info
        if self.vm.is_networked():
            self.networking_groupbox.setEnabled(True);
            self.ip_label.setText(self.vm.ip if self.vm.ip is not None else "none")
            self.netmask_label.setText(self.vm.netmask if self.vm.netmask is not None else "none")
            self.gateway_label.setText(self.vm.gateway if self.vm.gateway is not None else "none")
        else:
            self.networking_groupbox.setEnabled(False);

        #max priv storage
        self.priv_img_size = self.vm.get_private_img_sz()/1024/1024
        self.max_priv_storage.setMinimum(self.priv_img_size)
        self.max_priv_storage.setValue(self.priv_img_size)


    def __apply_basic_tab__(self):
        msg = []

        # vmname changed
        vmname = str(self.vmname.text())
        if self.vm.name != vmname:
            if self.vm.is_running():
                msg.append("Can't change name of a running VM.")
            elif self.qvm_collection.get_vm_by_name(vmname) is not None:
                msg.append("Can't change VM name - a VM named <b>{0}</b> already exists in the system!".format(vmname))
            else:
                oldname = self.vm.name
                try:
                    self.vm.set_name(vmname)
                    self.anything_changed = True
                except Exception as ex:
                    msg.append(str(ex))
                    
        #vm label changed
        if self.vmlabel.currentIndex() != self.label_idx:
            label = self.label_list[self.vmlabel.currentIndex()]
            self.vm.label = label
            self.anything_changed = True

        #vm template changed
        if self.template_name.currentIndex() != self.template_idx:
            new_template_name = self.template_name.currentText()
            new_template_name = new_template_name.split(' ')[0]
            template_vm = self.qvm_collection.get_vm_by_name(new_template_name)
            assert (template_vm is not None and template_vm.qid in self.qvm_collection)
            assert template_vm.is_template()
            self.vm.template = template_vm
            self.anything_changed = True

        #vm netvm changed
        if self.netVM.currentIndex() != self.netvm_idx:
            new_netvm_name = self.netVM.currentText()
            new_netvm_name = new_netvm_name.split(' ')[0]
    
            uses_default_netvm = False

            if new_netvm_name == "default":
                new_netvm_name = self.qvm_collection.get_default_netvm().name
                uses_default_netvm = True

            if new_netvm_name == "none":
                netvm = None
            else:
                netvm = self.qvm_collection.get_vm_by_name(new_netvm_name)
            assert (netvm is None or (netvm is not None and netvm.qid in self.qvm_collection and netvm.is_netvm()))
            
            self.vm.netvm = netvm
            self.vm.uses_default_netvm = uses_default_netvm
            self.anything_changed = True

        #include in backups
        if self.vm.include_in_backups != self.include_in_backups.isChecked():
            self.vm.include_in_backups = self.include_in_backups.isChecked()

        #max priv storage
        priv_size = self.max_priv_storage.value()
        if self.priv_img_size != priv_size:
            try:
                self.vm.resize_private_img(priv_size*1024*1024)
                self.anything_changed = True
            except Exception as ex:
                msg.append(str(ex))


        return msg


    ######### advanced tab

    def __init_advanced_tab__(self):

        #mem/cpu
        self.init_mem.setValue(int(self.vm.memory))
        self.init_mem.setMaximum(int(self.vm.maxmem))

        self.max_mem_size.setValue(int(self.vm.maxmem))
        self.max_mem_size.setMaximum(QubesHost().memory_total/1024)

        self.vcpus.setMinimum(1);
        self.vcpus.setMaximum(QubesHost().no_cpus)
        self.vcpus.setValue(int(self.vm.vcpus))

        self.include_in_balancing.setChecked('meminfo-writer' in self.vm.services and self.vm.services['meminfo-writer']==True)

        #kernel
        if self.vm.template is not None:
            text = self.vm.kernel
            self.kernel.insertItem(0, text)
            self.kernel.setEnabled(False)
            self.kernel_idx = 0
        else:
            text = "default (" + self.qvm_collection.get_default_kernel() +")"
            kernel_list = [text]
            for k in os.listdir(qubes_kernels_base_dir):
                kernel_list.append(k)
            kernel_list.append("none")

            self.kernel_idx = 0

            for (i, k) in enumerate(kernel_list):
                text = k
                if (text.startswith("default") and self.vm.uses_default_kernel) or ( self.vm.kernel == k and not self.vm.uses_default_kernel) or (k=="none" and self.vm.kernel==None):
                    text += " (current)"
                    self.kernel_idx = i
                self.kernel.insertItem(i,text)
            self.kernel.setCurrentIndex(self.kernel_idx)

        #kernel opts
        if self.vm.uses_default_kernelopts:
            self.kernel_opts.setText(self.vm.kernelopts + " (default)")
        else:
            self.kernel_opts.setText(self.vm.kernelopts)

                
        #paths
        self.dir_path.setText(self.vm.dir_path)
        self.config_path.setText(self.vm.conf_file)
        if self.vm.template is not None:
            self.root_img_path.setText(self.vm.template.root_img)
        else:
            self.root_img_path.setText("n/a")
        self.volatile_img_path.setText(self.vm.volatile_img)
        self.private_img_path.setText(self.vm.private_img)

    def __apply_advanced_tab__(self):

        #mem/cpu
        if self.init_mem.value() != int(self.vm.memory):
            self.vm.memory = self.init_mem.value()
            self.anything_changed = True

        if self.max_mem_size.value() != int(self.vm.maxmem):
            self.vm.maxmem = self.max_mem_size.value()
            self.anything_changed = True

        if self.vcpus.value() != int(self.vm.vcpus):
            self.vm.vcpus = self.vcpus.value() 
            self.anything_changed = True

        balancing_was_checked = ('meminfo-writer' in self.vm.services and self.vm.services['meminfo-writer']==True)
        if self.include_in_balancing.isChecked() != balancing_was_checked:
            self.new_srv_dict['meminfo-writer'] = self.include_in_balancing.isChecked()
            self.anything_changed = True

        #kernel changed
        if self.kernel.currentIndex() != self.kernel_idx:
            new_kernel = self.kernel.currentText()
            new_kernel = new_kernel.split(' ')[0]
            if(new_kernel == "default"):
                kernel = self.qvm_collection.get_default_kernel()
                self.vm.uses_default_kernel = True
            elif(new_kernel == "none"):
                kernel = None
                self.vm.uses_default_kernel = False;
            else:
                kernel = new_kernel
                self.vm.uses_default_kernel = False;

            self.vm.kernel = kernel
            self.anything_changed = True

    ######## devices tab
    def __init_devices_tab__(self):
        self.dev_list = MultiSelectWidget(self)
        self.devices_layout.addWidget(self.dev_list)
        
        devs = []
        lspci = subprocess.Popen(["lspci",], stdout = subprocess.PIPE)
        for dev in lspci.stdout:
            devs.append( (dev.rstrip(), dev.split(' ')[0]) )

        class DevListWidgetItem(QListWidgetItem):
            def __init__(self, name, slot, parent = None):
                super(DevListWidgetItem, self).__init__(name, parent)
                self.slot = slot

        for d in devs:
            if d[1] in self.vm.pcidevs:
                self.dev_list.selected_list.addItem( DevListWidgetItem(d[0], d[1]))
            else:
                self.dev_list.available_list.addItem( DevListWidgetItem(d[0], d[1]))

        if self.dev_list.selected_list.count() > 0 and self.include_in_balancing.isChecked():
            self.dmm_warning_adv.show()
            self.dmm_warning_dev.show()
        else:
            self.dmm_warning_adv.hide()
            self.dmm_warning_dev.hide()


    def __apply_devices_tab__(self):
        sth_changed = False
        added = []

        for i in range(self.dev_list.selected_list.count()):
            item = self.dev_list.selected_list.item(i)
            if item.slot not in self.vm.pcidevs:
                added.append(item)
        
        if self.dev_list.selected_list.count() - len(added) < len(self.vm.pcidevs): #sth removed
            sth_changed = True;
        elif len(added) > 0:
            sth_changed = True;
        
        if sth_changed == True:
            pcidevs = []
            for i in range(self.dev_list.selected_list.count()):
                slot = self.dev_list.selected_list.item(i).slot
                pcidevs.append(slot)
            self.vm.pcidevs = pcidevs
            self.anything_changed = True

    def include_in_balancing_state_changed(self, state):
        if self.dev_list.selected_list.count() > 0:
            if state == QtCore.Qt.Checked:
                self.dmm_warning_adv.show()
                self.dmm_warning_dev.show()
            else:
                self.dmm_warning_adv.hide()
                self.dmm_warning_dev.hide()
    def devices_selection_changed(self):
        if self.include_in_balancing.isChecked():
            if self.dev_list.selected_list.count() > 0 :
                self.dmm_warning_adv.show()
                self.dmm_warning_dev.show()
            else:
                self.dmm_warning_adv.hide()
                self.dmm_warning_dev.hide()

    ######## services tab

    def __init_services_tab__(self):
        for srv in self.vm.services:
            item = QListWidgetItem(srv)
            if self.vm.services[srv] == True:
                item.setCheckState(QtCore.Qt.Checked)
            else:
                item.setCheckState(QtCore.Qt.Unchecked)
            self.services_list.addItem(item)
        self.new_srv_dict = copy(self.vm.services)

    def __add_service__(self):
        srv = str(self.service_line_edit.text()).strip()
        if srv != "" and srv not in self.new_srv_dict:
            item = QListWidgetItem(srv)
            item.setCheckState(QtCore.Qt.Checked)
            self.services_list.addItem(item)
            self.new_srv_dict[srv] = True

    def __remove_service__(self):
        row = self.services_list.currentRow()
        if row:
            item = self.services_list.takeItem(row)
            del self.new_srv_dict[str(item.text())]

    def __apply_services_tab__(self):
        new_dict = {}
        for r in range (self.services_list.count()):
            item = self.services_list.item(r)
            self.new_srv_dict[str(item.text())] = (item.checkState() == QtCore.Qt.Checked)
        
        if self.new_srv_dict != self.vm.services:
            self.vm.services = self.new_srv_dict
            self.anything_changed = True


    ######### firewall tab related

    def set_fw_model(self, model):
        self.fw_model = model
        self.rulesTreeView.setModel(model)
        self.rulesTreeView.header().setResizeMode(QHeaderView.ResizeToContents)
        self.rulesTreeView.header().setResizeMode(0, QHeaderView.Stretch)
        self.set_allow(model.allow)
        self.dnsCheckBox.setChecked(model.allowDns)
        self.icmpCheckBox.setChecked(model.allowIcmp)

    def set_allow(self, allow):
        self.policyAllowRadioButton.setChecked(allow)
        self.policyDenyRadioButton.setChecked(not allow)
 
    def new_rule_button_pressed(self):
        dialog = NewFwRuleDlg()
        self.run_rule_dialog(dialog)

    def edit_rule_button_pressed(self):
        dialog = NewFwRuleDlg()
        dialog.set_ok_enabled(True)
        selected = self.rulesTreeView.selectedIndexes()
        if len(selected) > 0:
            row = self.rulesTreeView.selectedIndexes().pop().row()
            address = self.fw_model.get_column_string(0, row).replace(' ', '')
            dialog.addressComboBox.setItemText(0, address)
            dialog.addressComboBox.setCurrentIndex(0)
            service = self.fw_model.get_column_string(1, row)
            if service == "any":
                service = ""
            dialog.serviceComboBox.setItemText(0, service)
            dialog.serviceComboBox.setCurrentIndex(0)
            protocol = self.fw_model.get_column_string(2, row)
            if protocol == "tcp":
                dialog.tcp_radio.setChecked(True)
            elif protocol == "udp":
                dialog.udp_radio.setChecked(True)
            else:
                dialog.any_radio.setChecked(True)

            self.run_rule_dialog(dialog, row)

    def delete_rule_button_pressed(self):
        for i in set([index.row() for index in self.rulesTreeView.selectedIndexes()]):
            self.fw_model.removeChild(i)
            self.fw_model.fw_changed = True

    def run_rule_dialog(self, dialog, row = None):
        if dialog.exec_():
            address = str(dialog.addressComboBox.currentText())
            service = str(dialog.serviceComboBox.currentText())
            port = None
            port2 = None

            unmask = address.split("/", 1)
            if len(unmask) == 2:
                address = unmask[0]
                netmask = int(unmask[1])
            else:
                netmask = 32

            if address == "*":
                address = "0.0.0.0"
                netmask = 0

            if dialog.any_radio.isChecked():
                protocol = "any"
                port = 0
            else:
                if dialog.tcp_radio.isChecked():
                    protocol = "tcp"
                elif dialog.udp_radio.isChecked():
                    protocol = "udp"
                          
                try:
                    range = service.split("-", 1)
                    if len(range) == 2:
                        port = int(range[0])
                        port2 = int(range[1])
                    else:
                        port = int(service)
                except (TypeError, ValueError) as ex:
                    port = self.fw_model.get_service_port(service)

            if port is not None:
                if port2 is not None and port2 <= port:
                    QMessageBox.warning(None, "Invalid service ports range", "Port {0} is lower than port {1}.".format(port2, port))
                else:
                    item = QubesFirewallRuleItem(address, netmask, port, port2, protocol)
                    if row is not None:
                        self.fw_model.setChild(row, item)
                        self.fw_model.fw_changed = True
                    else:
                        self.fw_model.appendChild(item)
                        self.fw_model.fw_changed = True
            else:
                QMessageBox.warning(None, "Invalid service name", "Service '{0} is unknown.".format(service))


# Bases on the original code by:
# Copyright (c) 2002-2007 Pascal Varet <p.varet@gmail.com>

def handle_exception( exc_type, exc_value, exc_traceback ):
    import sys
    import os.path
    import traceback

    filename, line, dummy, dummy = traceback.extract_tb( exc_traceback ).pop()
    filename = os.path.basename( filename )
    error    = "%s: %s" % ( exc_type.__name__, exc_value )

    strace = ""
    stacktrace = traceback.extract_tb( exc_traceback )
    while len(stacktrace) > 0:
        (filename, line, func, txt) = stacktrace.pop()
        strace += "----\n"
        strace += "line: %s\n" %txt
        strace += "func: %s\n" %func
        strace += "line no.: %d\n" %line
        strace += "file: %s\n" %filename

    msg_box = QMessageBox()
    msg_box.setDetailedText(strace)
    msg_box.setIcon(QMessageBox.Critical)
    msg_box.setWindowTitle( "Houston, we have a problem...")
    msg_box.setText("Whoops. A critical error has occured. This is most likely a bug "
                    "in Qubes Manager.<br><br>"
                    "<b><i>%s</i></b>" % error +
                    "<br/>at line <b>%d</b><br/>of file %s.<br/><br/>"
                    % ( line, filename ))
    
    msg_box.exec_()


def main():

    global qubes_host
    qubes_host = QubesHost()

    global app
    app = QApplication(sys.argv)
    app.setOrganizationName("The Qubes Project")
    app.setOrganizationDomain("http://qubes-os.org")
    app.setApplicationName("Qubes VM Settings")

    sys.excepthook = handle_exception

    qvm_collection = QubesVmCollection()
    qvm_collection.lock_db_for_reading()
    qvm_collection.load()
    qvm_collection.unlock_db()

    vm = None

    if len(sys.argv) > 1:
        vm = qvm_collection.get_vm_by_name(sys.argv[1])
        if vm is None or vm.qid not in qvm_collection:
            QMessageBox.critical(None, "Qubes VM Settings Error",
                    "A VM with the name '{0}' does not exist in the system.".format(sys.argv[1]))
            sys.exit(1)
    else:
        vms_list = [vm.name for vm in qvm_collection.values() if (vm.is_appvm() or vm.is_template())]
        vmname = QInputDialog.getItem(None, "Select VM", "Select VM:", vms_list, editable = False)
        if not vmname[1]:
            sys.exit(1)
        vm = qvm_collection.get_vm_by_name(vmname[0])


    global settings_window
    settings_window = VMSettingsWindow(vm, app, qvm_collection, "basic")

    settings_window.show()

    app.exec_()
    app.exit()


if __name__ == "__main__":
    main()