#+
# Copyright 2011 iXsystems, Inc.
# All rights reserved
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted providing that the following conditions
# are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR ``AS IS'' AND ANY EXPRESS OR
# IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
# OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
# STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING
# IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
#
#####################################################################
from collections import namedtuple
import json
import logging
import os
import subprocess

from django.shortcuts import render
from django.http import HttpResponse
from django.utils.translation import ugettext as _

import eventlet
from freenasUI.freeadmin.middleware import public
from freenasUI.freeadmin.views import JsonResp
from freenasUI.jails.models import Jails, JailsConfiguration
from freenasUI.jails.utils import (
    jail_path_configured, jail_auto_configure, guess_adresses,
    new_default_plugin_jail
)
from freenasUI.middleware.exceptions import MiddlewareError
from freenasUI.middleware.notifier import notifier
from freenasUI.plugins import models, forms, availablePlugins
from freenasUI.plugins.plugin import PROGRESS_FILE
from freenasUI.plugins.utils import get_base_url, get_plugin_status
#from freenasUI.plugins.utils.fcgi_client import FCGIApp

import freenasUI.plugins.api_calls

log = logging.getLogger('plugins.views')


def home(request):

    conf = models.Configuration.objects.latest('id')
    return render(request, "plugins/index.html", {
        'conf': conf,
    })


def plugins(request):

    Service = namedtuple('Service', [
        'name',
        'status',
        'pid',
        'start_url',
        'stop_url',
        'status_url',
        'jail_status',
    ])

    host = get_base_url(request)
    plugins = models.Plugins.objects.filter(plugin_enabled=True)
    args = map(lambda y: (y, host, request), plugins)

    pool = eventlet.GreenPool(20)
    for plugin, _json, jail_status in pool.imap(get_plugin_status, args):

        if not _json:
            _json = {}
            _json['status'] = None

        plugin.service = Service(
            name=plugin.plugin_name,
            status=_json['status'],
            pid=_json.get("pid", None),
            start_url="/plugins/%s/%d/_s/start" % (
                plugin.plugin_name, plugin.id
            ),
            stop_url="/plugins/%s/%d/_s/stop" % (
                plugin.plugin_name, plugin.id
            ),
            status_url="/plugins/%s/%d/_s/status" % (
                plugin.plugin_name, plugin.id
            ),
            jail_status=jail_status,
        )

    return render(request, "plugins/plugins.html", {
        'plugins': plugins,
    })


def plugin_edit(request, plugin_id):
    plugin = models.Plugins.objects.filter(id=plugin_id)[0]

    if request.method == 'POST':
        plugins_form = forms.PluginsForm(request.POST, instance=plugin)
        if plugins_form.is_valid():
            plugins_form.save()
            return JsonResp(request, message=_("Plugin successfully updated."))
        else:
            plugin = None

    else:
        plugins_form = forms.PluginsForm(instance=plugin)

    return render(request, 'plugins/plugin_edit.html', {
        'plugin_id': plugin_id,
        'form': plugins_form
    })


def plugin_info(request, plugin_id):
    plugin = models.Plugins.objects.filter(id=plugin_id)[0]
    return render(request, 'plugins/plugin_info.html', {
        'plugin': plugin,
    })


def plugin_update(request, plugin_id):
    plugin_id = int(plugin_id)
    plugin = models.Plugins.objects.get(id=plugin_id)

    plugin_upload_path = notifier().get_plugin_upload_path()
    notifier().change_upload_location(plugin_upload_path)

    if request.method == "POST":
        form = forms.PBIUpdateForm(request.POST, request.FILES, plugin=plugin)
        if form.is_valid():
            form.done()
            return JsonResp(
                request,
                message=_('Plugin successfully updated'),
                events=['reloadHttpd()'],
            )
        else:
            resp = render(request, "plugins/plugin_update.html", {
                'form': form,
            })
            resp.content = (
                "<html><body><textarea>"
                + resp.content +
                "</textarea></boby></html>"
            )
            return resp
    else:
        form = forms.PBIUpdateForm(plugin=plugin)

    return render(request, "plugins/plugin_update.html", {
        'form': form,
    })


def install_available(request, oid):

    try:
        if not jail_path_configured():
            jail_auto_configure()
        addrs = guess_adresses()
        if not addrs['high_ipv4']:
            raise MiddlewareError(_(
                "You must configure your network interface and a default "
                "gateway"))
    except MiddlewareError, e:
        return render(request, "plugins/install_error.html", {
            'error': e.value,
        })

    jc = JailsConfiguration.objects.order_by("-id")[0]
    logfile = '%s/warden.log' % jc.jc_path
    if os.path.exists(logfile):
        os.unlink(logfile)
    if os.path.exists("/tmp/.plugin_upload_install"):
        os.unlink("/tmp/.plugin_upload_install")
    if os.path.exists("/tmp/.jailcreate"):
        os.unlink("/tmp/.jailcreate")
    if os.path.exists(PROGRESS_FILE):
        os.unlink(PROGRESS_FILE)

    plugin = None
    conf = models.Configuration.objects.latest('id')
    if conf and conf.collectionurl:
        url = conf.collectionurl
    else:
        url = models.PLUGINS_INDEX
    for p in availablePlugins.get_remote(url=url, cache=True):
        if p.id == int(oid):
            plugin = p
            break

    if not plugin:
        raise MiddlewareError(_("Invalid plugin"))

    if request.method == "POST":

        plugin_upload_path = notifier().get_plugin_upload_path()
        notifier().change_upload_location(plugin_upload_path)

        if not plugin.download("/var/tmp/firmware/pbifile.pbi"):
            raise MiddlewareError(_("Failed to download plugin"))

        jail = new_default_plugin_jail(plugin.unixname)

        newplugin = []
        if notifier().install_pbi(jail.jail_host, newplugin):
            newplugin = newplugin[0]
            notifier()._restart_plugins(
                newplugin.plugin_jail,
                newplugin.plugin_name,
            )
        else:
            jail.delete()

        return JsonResp(
            request,
            message=_("Plugin successfully installed"),
            events=['reloadHttpd()'],
        )

    proc = subprocess.Popen(
        "/usr/local/bin/warden template list|grep pluginjail",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=True,
    )
    notemplate = proc.wait() != 0

    return render(request, "plugins/available_install.html", {
        'plugin': plugin,
        'notemplate': notemplate,
    })


def install_progress(request):

    jc = JailsConfiguration.objects.order_by("-id")[0]
    logfile = '%s/warden.log' % jc.jc_path

    percent = 0.0
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, 'r') as f:
            try:
                percent = int(f.readlines()[-1].strip())
            except:
                pass
        percent = percent * 0.2

    if os.path.exists(logfile):
        percent = 0
        with open(logfile, 'r') as f:
            for line in f.xreadlines():
                if line.startswith('====='):
                    parts = line.split()
                    if len(parts) > 1:
                        try:
                            percent = int(parts[1][:-1])
                        except:
                            pass

        if not percent:
            percent = 0
        percent = percent * 0.7
        percent += 20

    if os.path.exists("/tmp/.plugin_upload_install"):
        percent = 100

    return HttpResponse(
        'new Object({state: "uploading", received: %s, size: 100});' % (
            int(percent),
        ))


def upload(request, jail_id=-1):

    #FIXME: duplicated code with available_install
    try:
        if not jail_path_configured():
            jail_auto_configure()
        addrs = guess_adresses()
        if not addrs['high_ipv4']:
            raise MiddlewareError(_(
                "You must configure your network interface and a default "
                "gateway"))
    except MiddlewareError, e:
        return render(request, "plugins/install_error.html", {
            'error': e.value,
        })

    jc = JailsConfiguration.objects.order_by("-id")[0]
    logfile = '%s/warden.log' % jc.jc_path
    if os.path.exists(logfile):
        os.unlink(logfile)
    if os.path.exists("/tmp/.plugin_upload_install"):
        os.unlink("/tmp/.plugin_upload_install")
    if os.path.exists("/tmp/.jailcreate"):
        os.unlink("/tmp/.jailcreate")

    plugin_upload_path = notifier().get_plugin_upload_path()
    notifier().change_upload_location(plugin_upload_path)

    jail = None
    if jail_id > 0:
        try:
            jail = Jails.objects.filter(pk=jail_id)[0]

        except Exception, e:
            log.debug("Failed to get jail %d: %s", jail_id, repr(e))
            jail = None

    if request.method == "POST":

        form = forms.PBIUploadForm(request.POST, request.FILES, jail=jail)
        if form.is_valid():
            form.done()
            return JsonResp(
                request,
                message=_('Plugin successfully installed'),
                events=['reloadHttpd()'],
            )
        else:
            resp = render(request, "plugins/plugin_install.html", {
                'form': form,
            })
            resp.content = (
                "<html><body><textarea>"
                + resp.content +
                "</textarea></boby></html>"
            )
            return resp
    else:
        form = forms.PBIUploadForm(jail=jail)

    proc = subprocess.Popen(
        "/usr/local/bin/warden template list|grep pluginjail",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=True,
    )
    notemplate = proc.wait() != 0

    return render(request, "plugins/upload.html", {
        'form': form,
        'notemplate': notemplate,
    })


def upload_nojail(request):
    return upload(request)


def upload_progress(request):
    import requests
    jc = JailsConfiguration.objects.order_by("-id")[0]
    logfile = '%s/warden.log' % jc.jc_path

    percent = 0.0

    progid = request.META.get("X-Progress-ID")
    try:
        r = requests.get("http://localhost/progress", headers={
            "X-Progress-ID": progid,
        }, timeout=0.7)
        data = json.loads(r.text[1:-2])
        percent = float(data['received']) / int(data['size'])
        percent = percent * 0.1

    except Exception, e:
        pass

    if os.path.exists(logfile):
        percent = 0
        with open(logfile, 'r') as f:
            for line in f.xreadlines():
                if line.startswith('====='):
                    parts = line.split()
                    if len(parts) > 1:
                        try:
                            percent = int(parts[1][:-1])
                        except:
                            pass

        if not percent:
            percent = 0
        percent = percent * 0.8
        percent += 10

    if os.path.exists("/tmp/.plugin_upload_install"):
        percent = 100

    return HttpResponse(
        'new Object({state: "uploading", received: %s, size: 100});' % (
            int(percent),
        ))


@public
def plugin_fcgi_client(request, name, path):
    log.debug("XXX: plugin_fcgi_client: reqest = %s", request)
    log.debug("XXX: plugin_fcgi_client: name = %s", name)
    log.debug("XXX: plugin_fcgi_client: path = %s", path)
#    """
#    This is a view that works as a FCGI client
#    It is used for development server (no nginx) for easier development
#
#    XXX
#    XXX - This will no longer work with multiple jail model
#    XXX
#    """
#    qs = models.Plugins.objects.filter(plugin_name=name)
#    if not qs.exists():
#        raise Http404
#
#    plugin = qs[0]
#    jail_ip = PluginsJail.objects.order_by('-id')[0].jail_ipv4address
#
#    app = FCGIApp(host=str(jail_ip), port=plugin.plugin_port)
#    env = request.META.copy()
#    env.pop('wsgi.file_wrapper', None)
#    env.pop('wsgi.version', None)
#    env.pop('wsgi.input', None)
#    env.pop('wsgi.errors', None)
#    env.pop('wsgi.multiprocess', None)
#    env.pop('wsgi.run_once', None)
#    env['SCRIPT_NAME'] = env['PATH_INFO']
#    args = request.POST if request.method == "POST" else request.GET
#    status, headers, body, raw = app(env, args=args)
#
#    resp = HttpResponse(body)
#    for header, value in headers:
#        resp[header] = value
#    return resp
