# -*- coding: utf-8 -*-

import asyncio
from aiohttp.client_exceptions import ClientOSError, ClientResponseError
from gi.repository import GLib
from datetime import datetime, timedelta
import hashlib
import os
import os.path
import re
import logging

from .dbus_client import AsyncDBUSClient
from .ddi.client import DDIClient, APIError
from .ddi.client import (
    ConfigStatusExecution, ConfigStatusResult)
from .ddi.deployment_base import (
    DeploymentStatusExecution, DeploymentStatusResult)
from .ddi.cancel_action import (
    CancelStatusExecution, CancelStatusResult)


class RaucDBUSDDIClient(AsyncDBUSClient):
    """
    Client broker communicating with RAUC via DBUS and HawkBit DDI HTTP
    interface.
    """
    def __init__(self, session, host, ssl, tenant_id, target_name, auth_token,
                 attributes, bundle_dl_location, result_callback, step_callback=None, lock_keeper=None):
        super(RaucDBUSDDIClient, self).__init__()

        self.attributes = attributes

        self.logger = logging.getLogger('rauc_hawkbit')
        self.ddi = DDIClient(session, host, ssl, auth_token, tenant_id, target_name)
        self.action_id = None

        bundle_dir = os.path.dirname(bundle_dl_location)
        assert os.path.isdir(bundle_dir), 'Bundle directory must exist'
        assert os.access(bundle_dir, os.W_OK), 'Bundle directory not writeable'

        self.bundle_dl_location = bundle_dl_location
        self.lock_keeper = lock_keeper
        self.result_callback = result_callback
        self.step_callback = step_callback

        # DBUS proxy
        self.rauc = self.new_proxy('de.pengutronix.rauc.Installer', '/')

        # DBUS property/signal subscription
        self.new_property_subscription('de.pengutronix.rauc.Installer',
                                       'Progress', self.progress_callback)
        self.new_property_subscription('de.pengutronix.rauc.Installer',
                                       'LastError', self.last_error_callback)
        self.new_signal_subscription('de.pengutronix.rauc.Installer',
                                     'Completed', self.complete_callback)

    async def complete_callback(self, connection, sender_name, object_path,
                                interface_name, signal_name, parameters):
        """Callback for completion."""

        if self.lock_keeper:
            self.lock_keeper.unlock(self)

        result = parameters[0]
        self.result_callback(result)

        # bundle update was triggered from elsewhere than hawkbit
        if not self.action_id:
            return
        try:
            os.remove(self.bundle_dl_location)
        except BaseException as e:
            self.logger.warning('Error removing update bundle')
            self.logger.warning(str(e))
        status_msg = 'Rauc bundle update completed with result: {}'.format(
            result)
        self.logger.info(status_msg)

        # send feedback to HawkBit
        if result == 0:
            status_execution = DeploymentStatusExecution.closed
            status_result = DeploymentStatusResult.success
        else:
            status_execution = DeploymentStatusExecution.closed
            status_result = DeploymentStatusResult.failure

        await self.ddi.deploymentBase[self.action_id].feedback(
                status_execution, status_result, [status_msg])

        self.action_id = None

    async def progress_callback(self, connection, sender_name,
                                object_path, interface_name,
                                signal_name, parameters):
        """Callback for changed Progress property."""

        percentage, description, nesting_depth = parameters
        self.logger.info(
            'Update progress: {}% {}'.format(percentage, description)
        )

        if self.step_callback:
            self.step_callback(percentage, description)

        # bundle update was triggered from elsewhere than hawkbit
        if not self.action_id:
            return

        # send feedback to HawkBit
        status_execution = DeploymentStatusExecution.proceeding
        status_result = DeploymentStatusResult.none
        await self.ddi.deploymentBase[self.action_id].feedback(
                status_execution, status_result, [description],
                percentage=percentage)

    async def last_error_callback(self, connection, sender_name,
                                  object_path, interface_name,
                                  signal_name, last_error):
        """Callback for changed LastError property."""
        # bundle update was triggered from elsewhere
        if not self.action_id:
            return

        # LastError property might have been cleared
        if not last_error:
            return

        self.logger.info('Last error: {}'.format(last_error))

        # send feedback to HawkBit
        status_execution = DeploymentStatusExecution.proceeding
        status_result = DeploymentStatusResult.failure
        await self.ddi.deploymentBase[self.action_id].feedback(
                status_execution, status_result, [last_error])

    async def start_polling(self, wait_on_error=60):
        """Wrapper around self.poll_base_resource() for exception handling."""
        while True:
            try:
                await self.poll_base_resource()
            except asyncio.CancelledError:
                self.logger.info('Polling cancelled')
                break
            except asyncio.TimeoutError:
                self.logger.warning('Polling failed due to TimeoutError')
            except (APIError, TimeoutError, ClientOSError, ClientResponseError) as e:
                # log error and start all over again
                self.logger.warning('Polling failed with a temporary error: {}'.format(e))
            except Exception:
                self.logger.exception('Polling failed with an unexpected exception:')
            self.action_id = None
            self.logger.info('Retry will happen in {} seconds'.format(
                wait_on_error))
            await asyncio.sleep(wait_on_error)

    async def identify(self, base):
        """Identify target against HawkBit."""
        self.logger.info('Sending identifying information to HawkBit')
        compatible_string = self.rauc.get_cached_property('Compatible').get_string()
        # identify
        await self.ddi.configData(
                ConfigStatusExecution.closed,
                ConfigStatusResult.success, compatible=compatible_string, **self.attributes)

    async def reject_cancel(self, base):
        self.logger.info('Received cancelation request')
        # retrieve action id from URL
        deployment = base['_links']['cancelAction']['href']
        match = re.search('/cancelAction/(.+)$', deployment)
        action_id, = match.groups()
        # retrieve stop_id
        stop_info = await self.ddi.cancelAction[action_id]()
        stop_id = stop_info['cancelAction']['stopId']
        # Reject cancel request
        self.logger.info('Rejecting cancelation request')
        await self.ddi.cancelAction[stop_id].feedback(
                CancelStatusExecution.rejected, CancelStatusResult.success, status_details=("Cancelling not supported",))

    async def cancel(self, base):
        self.logger.info('Received cancelation request')
        # retrieve action id from URL
        deployment = base['_links']['cancelAction']['href']
        match = re.search('/cancelAction/(.+)$', deployment)
        action_id, = match.groups()
        # retrieve stop_id
        stop_info = await self.ddi.cancelAction[action_id]()
        stop_id = stop_info['cancelAction']['stopId']
        # Reject cancel request
        self.logger.info('Accept cancelation request')
        await self.ddi.cancelAction[stop_id].feedback(
                CancelStatusExecution.closed, CancelStatusResult.success, status_details=("Cancelled",))

    async def install(self, custom_bundle_location=None):
        if self.lock_keeper and not self.lock_keeper.lock(self):
            self.logger.info("Another installation is already in progress, aborting")
            return

        self.rauc.Install('(s)', custom_bundle_location or self.bundle_dl_location)

    async def process_deployment(self, base):
        """
        Check for deployments, download them, verify checksum and trigger
        RAUC install operation.
        """
        action_id, deploy_info = await self.retrieve_deployment_information(base)
        await self.process_download(action_id, deploy_info)
        await self.process_installation()

    async def retrieve_deployment_information(self, base):
        # retrieve action id and resource parameter from URL
        deployment = base['_links']['deploymentBase']['href']
        match = re.search('/deploymentBase/(.+)\\?c=(.+)$', deployment)
        action_id, resource = match.groups()
        self.logger.info('Deployment found for this target')
        # fetch deployment information
        deploy_info = await self.ddi.deploymentBase[action_id](resource)
        return action_id, deploy_info


    async def process_download(self, action_id, deploy_info):
        """
        Check for deployments, download them and verify checksum.
        """
        if self.action_id is not None:
            self.logger.info('Deployment is already in progress')
            return

        try:
            chunk = deploy_info['deployment']['chunks'][0]
        except IndexError:
            # send negative feedback to HawkBit
            status_execution = DeploymentStatusExecution.closed
            status_result = DeploymentStatusResult.failure
            msg = 'Deployment without chunks found. Ignoring'
            await self.ddi.deploymentBase[action_id].feedback(
                    status_execution, status_result, [msg])
            raise APIError(msg)

        try:
            artifact = chunk['artifacts'][0]
        except IndexError:
            # send negative feedback to HawkBit
            status_execution = DeploymentStatusExecution.closed
            status_result = DeploymentStatusResult.failure
            msg = 'Deployment without artifacts found. Ignoring'
            await self.ddi.deploymentBase[action_id].feedback(
                    status_execution, status_result, [msg])
            raise APIError(msg)

        # prefer https ('download') over http ('download-http')
        # HawkBit provides either only https, only http or both
        if 'download' in artifact['_links']:
            download_url = artifact['_links']['download']['href']
        else:
            download_url = artifact['_links']['download-http']['href']

        # download artifact, check md5 and report feedback
        md5_hash = artifact['hashes']['md5']
        self.logger.info('Starting bundle download')
        await self.download_artifact(action_id, download_url, md5_hash)

        self.action_id = action_id

    async def process_installation(self):
        """
        Trigger RAUC install operation for an already downloaded bundle.
        """
        self.logger.info('Starting installation')
        if self.action_id is None:
            self.logger.info('No Deployment in progress')
            return
        try:
            # do not interrupt install call
            await asyncio.shield(self.install())
        except GLib.Error as e:
            # send negative feedback to HawkBit
            status_execution = DeploymentStatusExecution.closed
            status_result = DeploymentStatusResult.failure
            await self.ddi.deploymentBase[self.action_id].feedback(
                    status_execution, status_result, [str(e)])
            raise APIError(str(e))

    def bundle_already_downloaded(self, bundle_location, md5):
        self.logger.debug(f'Checking if file with md5 {md5} already exists')

        do_hashes_match = False
        try:
            hash_md5 = hashlib.md5()
            with open(bundle_location, "rb") as f:
                for chunk in iter(lambda: f.read(4096), b""):
                    hash_md5.update(chunk)
            self.logger.debug(f'existing bundle hash: {hash_md5.hexdigest()} vs hawkbit: {md5}')
            do_hashes_match = hash_md5.hexdigest() == md5
        finally:
            return do_hashes_match

    async def download_artifact(self, action_id, url, md5sum,
                                tries=3):
        """Download bundle artifact."""
        try:
            match = re.search('/softwaremodules/(.+)/artifacts/(.+)$', url)
            software_module, filename = match.groups()
            static_api_url = False
        except AttributeError:
            static_api_url = True

        if self.step_callback:
            self.step_callback(0, "Downloading bundle...")

        bundle_exists = self.bundle_already_downloaded(self.bundle_dl_location, md5sum)
        if bundle_exists:
            self.logger.info("Bundle already on disk .. skipping download")
            return

        # As rauc removes write permissions to the file when installing
        # we need to delete any existing bundles before starting to write to the file
        try:
            os.remove(self.bundle_dl_location)
        except FileNotFoundError:
            pass

        # try several times
        for dl_try in range(tries):
            if not static_api_url:
                checksum = await self.ddi.softwaremodules[software_module] \
                    .artifacts[filename](self.bundle_dl_location)
            else:
                # API implementations might return static URLs, so bypass API
                # methods and download bundle anyway
                checksum = await self.ddi.get_binary(url,
                                                     self.bundle_dl_location)

            if checksum == md5sum:
                self.logger.info('Download successful')
                return
            else:
                self.logger.error('Checksum does not match. {} tries remaining'
                                  .format(tries-dl_try))
        # MD5 comparison unsuccessful, send negative feedback to HawkBit
        status_msg = 'Artifact checksum does not match after {} tries.' \
            .format(tries)
        status_execution = DeploymentStatusExecution.closed
        status_result = DeploymentStatusResult.failure
        await self.ddi.deploymentBase[action_id].feedback(
                status_execution, status_result, [status_msg])
        raise APIError(status_msg)

    async def sleep(self, base):
        """Sleep time suggested by HawkBit."""
        sleep_str = base['config']['polling']['sleep']
        self.logger.info('Will sleep for {}'.format(sleep_str))
        t = datetime.strptime(sleep_str, '%H:%M:%S')
        delta = timedelta(hours=t.hour, minutes=t.minute, seconds=t.second)
        await asyncio.sleep(delta.total_seconds())

    async def poll_base_resource(self):
        """Poll DDI API base resource."""
        while True:
            base = await self.ddi()

            if '_links' in base:
                if 'configData' in base['_links']:
                    await self.identify(base)
                if 'deploymentBase' in base['_links']:
                    await self.process_deployment(base)
                if 'cancelAction' in base['_links']:
                    await self.reject_cancel(base)

            await self.sleep(base)
