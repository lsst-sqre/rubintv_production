# This file is part of rubintv_production.
#
# Developed for the LSST Data Management System.
# This product includes software developed by the LSST Project
# (https://www.lsst.org).
# See the COPYRIGHT file at the top-level directory of this distribution
# for details of code ownership.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import json
import os
import time
import logging
import tempfile
from glob import glob
from time import sleep
import numpy as np
import matplotlib.pyplot as plt

import lsst.summit.utils.butlerUtils as butlerUtils
from astro_metadata_translator import ObservationInfo
from lsst.obs.lsst.translators.lsst import FILTER_DELIMITER

from lsst.utils import getPackageDir
from lsst.pipe.tasks.characterizeImage import CharacterizeImageTask, CharacterizeImageConfig
from lsst.pipe.tasks.calibrate import CalibrateTask, CalibrateConfig
from lsst.meas.algorithms import ReferenceObjectLoader
import lsst.daf.butler as dafButler
from lsst.obs.base import DefineVisitsConfig, DefineVisitsTask
from lsst.pipe.base import Instrument

try:
    from lsst_efd_client import EfdClient
    HAS_EFD_CLIENT = True
except ImportError:
    HAS_EFD_CLIENT = False

try:
    from google.cloud import storage
    from google.cloud.storage.retry import DEFAULT_RETRY
    HAS_GOOGLE_STORAGE = True
except ImportError:
    HAS_GOOGLE_STORAGE = False

from lsst.summit.utils.bestEffort import BestEffortIsr
from lsst.summit.utils.imageExaminer import ImageExaminer
from lsst.summit.utils.spectrumExaminer import SpectrumExaminer

from lsst.summit.utils.utils import dayObsIntToString
from lsst.atmospec.utils import isDispersedDataId, isDispersedExp

from lsst.rubintv.production.mountTorques import (calculateMountErrors, MOUNT_IMAGE_WARNING_LEVEL,
                                                  MOUNT_IMAGE_BAD_LEVEL)
from lsst.rubintv.production.monitorPlotting import plotExp
from .channels import PREFIXES, CHANNELS
from .utils import writeMetadataShard, isFileWorldWritable, LocationConfig
from .base import FileWatcher

__all__ = [
    '_dataIdToFilename',
    'IsrRunner',
    'ImExaminerChannel',
    'SpecExaminerChannel',
    'MonitorChannel',
    'MountTorqueChannel',
    'MetadataServer',
    'Uploader',
    'Heartbeater',
    'CalibrateCcdRunner',
]


_LOG = logging.getLogger(__name__)

SIDECAR_KEYS_TO_REMOVE = ['instrument',
                          'obs_id',
                          'seq_start',
                          'seq_end',
                          'group_name',
                          'has_simulated',
                          ]

# The values here are used in HTML so do not include periods in them, eg "Dec."
MD_NAMES_MAP = {"id": 'Exposure id',
                "exposure_time": 'Exposure time',
                "dark_time": 'Darktime',
                "observation_type": 'Image type',
                "observation_reason": 'Observation reason',
                "day_obs": 'dayObs',
                "seq_num": 'seqNum',
                "group_id": 'Group id',
                "target_name": 'Target',
                "science_program": 'Science program',
                "tracking_ra": 'RA',
                "tracking_dec": 'Dec',
                "sky_angle": 'Sky angle',
                "azimuth": 'Azimuth',
                "zenith_angle": 'Zenith angle',
                "time_begin_tai": 'TAI',
                "filter": 'Filter',
                "disperser": 'Disperser',
                "airmass": 'Airmass',
                "focus_z": 'Focus-Z',
                "seeing": 'DIMM Seeing',
                "altitude": 'Altitude',
                }


def _dataIdToFilename(channel, dataId, extension='.png'):
    """Convert a dataId to a png filename.

    Parameters
    ----------
    channel : `str`
        The name of the RubinTV channel
    dataId : `dict`
        The dataId

    Returns
    -------
    filename : `str`
        The filename
    """
    dayObsStr = dayObsIntToString(dataId['day_obs'])
    filename = f"{PREFIXES[channel]}_dayObs_{dayObsStr}_seqNum_{dataId['seq_num']}{extension}"
    return filename


def _waitForDataProduct(butler, dataProduct, dataId, logger, maxTime=20):
    """Wait for a dataProduct to land inside a repo, timing out in maxTime.

    Parameters
    ----------
    butler : `lsst.daf.butler.Butler`
        The butler to use.
    dataProduct : `str`
        The dataProduct to wait for, e.g. postISRCCD or calexp etc
    logger : `logging.Logger`
        Logger
    maxTime : `int` or `float`
        The timeout, in seconds, to wait before giving up and returning None.

    Returns
    -------
    dataProduct : dataProduct or None
        Either the dataProduct being waiter for, or None if maxTime elapsed.
    """
    cadence = 0.25
    maxLoops = int(maxTime//cadence)
    for retry in range(maxLoops):
        if butlerUtils.datasetExists(butler, dataProduct, dataId):
            return butler.get(dataProduct, dataId)
        else:
            sleep(cadence)
    logger.warning(f'Waited {maxTime}s for {dataProduct} for {dataId} to no avail')
    return None


class IsrRunner():
    """Class to run isr for each image that lands in the repo.

    Runs isr via BestEffortIsr, and puts the result in the quickLook rerun.
    """

    def __init__(self, location):
        self.config = LocationConfig(location)
        self.bestEffort = BestEffortIsr()
        self.log = _LOG.getChild("isrRunner")
        self.watcher = FileWatcher(location, 'raw', 'auxtel_isr_runner')

    def callback(self, dataId):
        """Method called on each new dataId as it is found in the repo.

        Produce a quickLookExp of the latest image, and butler.put() it to the
        repo so that downstream processes can find and use it.
        """
        quickLookExp = self.bestEffort.getExposure(dataId)  # noqa: F841 - automatically puts
        del quickLookExp
        self.log.info(f'Put quickLookExp for {dataId}, awaiting next image...')

    def run(self):
        """Run continuously, calling the callback method on the latest dataId.
        """
        self.watcher.run(self.callback)


class ImExaminerChannel():
    """Class for running the ImExam channel on RubinTV.
    """

    def __init__(self, location, doRaise=False):
        self.config = LocationConfig(location)
        self.dataProduct = 'quickLookExp'
        self.uploader = Uploader(self.config.bucketName)
        self.butler = butlerUtils.makeDefaultLatissButler()
        self.log = _LOG.getChild("imExaminerChannel")
        self.channel = 'summit_imexam'
        self.watcher = FileWatcher(location, self.dataProduct, self.channel)
        self.doRaise = doRaise

    def _imExamine(self, exp, dataId, outputFilename):
        if os.path.exists(outputFilename):  # unnecessary now we're using tmpfile
            self.log.warning(f"Skipping {outputFilename}")
            return
        imexam = ImageExaminer(exp, savePlots=outputFilename, doTweakCentroid=True)
        imexam.plot()

    def callback(self, dataId):
        """Method called on each new dataId as it is found in the repo.

        Plot the quick imExam analysis of the latest image, writing the plot
        to a temp file, and upload it to Google cloud storage via the uploader.
        """
        try:
            self.log.info(f'Running imexam on {dataId}')
            tempFilename = tempfile.mktemp(suffix='.png')
            uploadFilename = _dataIdToFilename(self.channel, dataId)
            exp = _waitForDataProduct(self.butler, self.dataProduct, dataId, self.log)

            if not exp:
                raise RuntimeError(f'Failed to get {self.dataProduct} for {dataId}')
            self._imExamine(exp, dataId, tempFilename)

            self.log.info("Uploading imExam to storage bucket")
            self.uploader.googleUpload(self.channel, tempFilename, uploadFilename)
            self.log.info('Upload complete')

        except Exception as e:
            if self.doRaise:
                raise RuntimeError(f"Error processing {dataId}") from e
            self.log.warning(f"Skipped imExam on {dataId} because {repr(e)}")
            return None

    def run(self):
        """Run continuously, calling the callback method on the latest dataId.
        """
        self.watcher.run(self.callback)


class SpecExaminerChannel():
    """Class for running the SpecExam channel on RubinTV.
    """

    def __init__(self, location, doRaise=False):
        self.config = LocationConfig(location)
        self.dataProduct = 'quickLookExp'
        self.uploader = Uploader(self.config.bucketName)
        self.butler = butlerUtils.makeDefaultLatissButler()
        self.log = _LOG.getChild("specExaminerChannel")
        self.channel = 'summit_specexam'
        self.watcher = FileWatcher(location, self.dataProduct, self.channel)
        self.doRaise = doRaise

    def _specExamine(self, exp, dataId, outputFilename):
        if os.path.exists(outputFilename):  # unnecessary now we're using tmpfile?
            self.log.warning(f"Skipping {outputFilename}")
            return
        summary = SpectrumExaminer(exp, savePlotAs=outputFilename)
        summary.run()

    def callback(self, dataId):
        """Method called on each new dataId as it is found in the repo.

        Plot the quick spectral reduction of the latest image, writing the plot
        to a temp file, and upload it to Google cloud storage via the uploader.
        """
        try:
            if not isDispersedDataId(dataId, self.butler):
                self.log.info(f'Skipping non dispersed image {dataId}')
                return

            self.log.info(f'Running specExam on {dataId}')
            tempFilename = tempfile.mktemp(suffix='.png')
            uploadFilename = _dataIdToFilename(self.channel, dataId)
            exp = _waitForDataProduct(self.butler, self.dataProduct, dataId, self.log)
            if not exp:
                raise RuntimeError(f'Failed to get {self.dataProduct} for {dataId}')
            self._specExamine(exp, dataId, tempFilename)

            self.log.info("Uploading specExam to storage bucket")
            self.uploader.googleUpload(self.channel, tempFilename, uploadFilename)
            self.log.info('Upload complete')

        except Exception as e:
            if self.doRaise:
                raise RuntimeError(f"Error processing {dataId}") from e
            self.log.info(f"Skipped imExam on {dataId} because {repr(e)}")
            return None

    def run(self):
        """Run continuously, calling the callback method on the latest dataId.
        """
        self.watcher.run(self.callback)


class MonitorChannel():
    """Class for running the monitor channel on RubinTV.
    """

    def __init__(self, location, doRaise=False):
        self.config = LocationConfig(location)
        self.dataProduct = 'quickLookExp'
        self.uploader = Uploader(self.config.bucketName)
        self.butler = butlerUtils.makeDefaultLatissButler()
        self.log = _LOG.getChild("monitorChannel")
        self.channel = 'auxtel_monitor'
        self.watcher = FileWatcher(location, self.dataProduct, self.channel)
        self.fig = plt.figure(figsize=(12, 12))
        self.doRaise = doRaise

    def _plotImage(self, exp, dataId, outputFilename):
        if os.path.exists(outputFilename):  # unnecessary now we're using tmpfile
            self.log.warning(f"Skipping {outputFilename}")
            return
        plotExp(exp, dataId, self.fig, outputFilename)

    def callback(self, dataId):
        """Method called on each new dataId as it is found in the repo.

        Plot the image for display on the monitor, writing the plot
        to a temp file, and upload it to Google cloud storage via the uploader.
        """
        try:
            self.log.info(f'Generating monitor image for {dataId}')
            tempFilename = tempfile.mktemp(suffix='.png')
            uploadFilename = _dataIdToFilename(self.channel, dataId)
            exp = _waitForDataProduct(self.butler, self.dataProduct, dataId, self.log)
            if not exp:
                raise RuntimeError(f'Failed to get {self.dataProduct} for {dataId}')
            self._plotImage(exp, dataId, tempFilename)

            self.log.info("Uploading monitor image to storage bucket")
            self.uploader.googleUpload(self.channel, tempFilename, uploadFilename)
            self.log.info('Upload complete')

        except Exception as e:
            if self.doRaise:
                raise RuntimeError(f"Error processing {dataId}") from e
            self.log.warning(f"Skipped monitor image for {dataId} because {repr(e)}")
            return None

    def run(self):
        """Run continuously, calling the callback method on the latest dataId.
        """
        self.watcher.run(self.callback)


class MountTorqueChannel():
    """Class for running the mount torque channel on RubinTV.
    """

    def __init__(self, location, doRaise=False):
        if not HAS_EFD_CLIENT:
            from lsst.summit.utils.utils import EFD_CLIENT_MISSING_MSG
            raise RuntimeError(EFD_CLIENT_MISSING_MSG)
        self.config = LocationConfig(location)
        self.dataProduct = 'raw'
        self.uploader = Uploader(self.config.bucketName)
        self.butler = butlerUtils.makeDefaultLatissButler()
        self.client = EfdClient('summit_efd')
        self.log = _LOG.getChild("mountTorqueChannel")
        self.channel = 'auxtel_mount_torques'
        self.shardsDir = self.config.metadataShardPath
        self.watcher = FileWatcher(location, self.dataProduct, self.channel)
        self.fig = plt.figure(figsize=(16, 16))
        self.doRaise = doRaise

    def writeMountErrorShard(self, errors, dataId):
        """Write a metadata shard for the mount error, including the flag
        for coloring the cell based on the threshold values.
        """
        dayObs = butlerUtils.getDayObs(dataId)
        seqNum = butlerUtils.getSeqNum(dataId)
        az_rms, el_rms, _ = errors  # we don't need the rot errors here so assign to _
        imageError = (az_rms ** 2 + el_rms ** 2) ** .5
        key = 'Mount motion image degradation'
        flagKey = '_' + key  # color coding of cells always done by prepending with an underscore
        contents = {key: imageError}

        if imageError > MOUNT_IMAGE_BAD_LEVEL:
            contents.update({flagKey: 'bad'})
        elif imageError > MOUNT_IMAGE_WARNING_LEVEL:
            contents.update({flagKey: 'warning'})

        md = {seqNum: contents}
        writeMetadataShard(self.shardsDir, dayObs, md)
        return

    def callback(self, dataId):
        """Method called on each new dataId as it is found in the repo.

        Plot the mount torques, pulling data from the EFD, writing the plot
        to a temp file, and upload it to Google cloud storage via the uploader.
        """
        try:
            tempFilename = tempfile.mktemp(suffix='.png')
            uploadFilename = _dataIdToFilename(self.channel, dataId)

            # calculateMountErrors() calculates the errors, but also performs
            # the plotting.
            errors = calculateMountErrors(dataId, self.butler, self.client, self.fig, tempFilename, self.log)

            if os.path.exists(tempFilename):  # skips many image types and short exps
                self.log.info("Uploading mount torque plot to storage bucket")
                self.uploader.googleUpload(self.channel, tempFilename, uploadFilename)
                self.log.info('Upload complete')

            # write the mount error shard, including the cell coloring flag
            if errors:  # if the mount torque fails or skips it returns False
                self.writeMountErrorShard(errors, dataId)

        except Exception as e:
            if self.doRaise:
                raise RuntimeError(f"Error processing {dataId}") from e
            self.log.warning(f"Skipped creating mount plots for {dataId} because {repr(e)}")

    def run(self):
        """Run continuously, calling the callback method on the latest dataId.
        """
        self.watcher.run(self.callback)


class MetadataServer():
    """Class for serving the metadata to the table on RubinTV.
    """

    def __init__(self, location, *, embargo=False, doRaise=False):
        self.config = LocationConfig(location)
        self.dataProduct = 'raw'
        self.uploader = Uploader(self.config.bucketName)
        self.butler = butlerUtils.makeDefaultLatissButler(embargo=embargo)
        self.log = _LOG.getChild("metadataServer")
        self.channel = 'auxtel_metadata'
        self.watcher = FileWatcher(location, self.dataProduct, self.channel)
        self.outputRoot = self.config.metadataPath
        self.shardsDir = self.config.metadataShardPath
        self.doRaise = doRaise
        for path in (self.outputRoot, self.shardsDir):  # should now be unnecessary with LocationConfig
            try:
                os.makedirs(path, exist_ok=True)
            except Exception as e:
                raise RuntimeError(f"Failed to find/create {path}") from e

    @staticmethod
    def dataIdToMetadataDict(butler, dataId, keysToRemove):
        """Create a dictionary of metadata for a dataId.

        Given a dataId, create a dictionary containing all metadata that should
        be displayed in the table on RubinTV. The table creation is dynamic,
        so any entries which should not appear as columns in the table should
        be removed via keysToRemove.

        Parameters
        ----------
        butler : `lsst.daf.butler.Butler`
            The butler.
        dataId : `dict`
            The dataId.
        keysToRemove : `list` [`str`]
            Keys to remove from the exposure record.

        Returns
        -------
        metadata : `dict` [`dict`]
            A dict, with a single key, corresponding to the dataId's seqNum,
            containing a dict of the dataId's metadata.
        """
        seqNum = butlerUtils.getSeqNum(dataId)
        expRecord = butlerUtils.getExpRecordFromDataId(butler, dataId)
        d = expRecord.toDict()

        time_begin_tai = expRecord.timespan.begin.to_datetime().strftime("%H:%M:%S")
        d['time_begin_tai'] = time_begin_tai
        d.pop('timespan')

        filt, disperser = d['physical_filter'].split(FILTER_DELIMITER)
        d.pop('physical_filter')
        d['filter'] = filt
        d['disperser'] = disperser

        rawmd = butler.get('raw.metadata', dataId)
        obsInfo = ObservationInfo(rawmd)
        d['airmass'] = obsInfo.boresight_airmass
        d['focus_z'] = obsInfo.focus_z.value

        d['altitude'] = None  # altaz_begin is None when not on sky so need check it's not None first
        if obsInfo.altaz_begin is not None:
            d['altitude'] = obsInfo.altaz_begin.alt.value

        if 'SEEING' in rawmd:  # SEEING not yet in the obsInfo so take direct from header
            d['seeing'] = rawmd['SEEING']

        for key in keysToRemove:
            if key in d:
                d.pop(key)

        properNames = {MD_NAMES_MAP[attrName]: d[attrName] for attrName in d}

        return {seqNum: properNames}

    def writeShardForDataId(self, dataId):
        """Write a standard shard for this dataId from the exposure record.

        Calls dataIdToMetadataDict to get the normal set of metadata components
        and then writes it to a shard file in the shards directory ready for
        upload.

        Parameters
        ----------
        dataId : `dict`
            The dataId.
        """
        md = self.dataIdToMetadataDict(self.butler, dataId, SIDECAR_KEYS_TO_REMOVE)
        dayObs = butlerUtils.getDayObs(dataId)
        writeMetadataShard(self.shardsDir, dayObs, md)
        return

    def mergeShardsAndUpload(self):
        """Merge all the shards in the shard directory into their respective
        files and upload the updated files.

        For each file found in the shard directory, merge its contents into the
        main json file for the corresponding dayObs, and for each file updated,
        upload it.
        """
        filesTouched = set()
        shardFiles = sorted(glob(os.path.join(self.shardsDir, "metadata-*")))
        if shardFiles:
            self.log.debug(f'Found {len(shardFiles)} shardFiles')
            sleep(0.1)  # just in case a shard is in the process of being written

        for shardFile in shardFiles:
            # filenames look like
            # metadata-dayObs_20221027_049a5f12-5b96-11ed-80f0-348002f0628.json
            filename = os.path.basename(shardFile)
            dayObs = int(filename.split("_", 2)[1])
            mainFile = self.getSidecarFilename(dayObs)
            filesTouched.add(mainFile)

            data = {}
            # json.load() doesn't like empty files so check size is non-zero
            if os.path.isfile(mainFile) and os.path.getsize(mainFile) > 0:
                with open(mainFile) as f:
                    data = json.load(f)

            with open(shardFile) as f:
                shard = json.load(f)
            if shard:
                for row in shard:
                    if row in data:
                        data[row].update(shard[row])
                    else:
                        data.update({row: shard[row]})
            os.remove(shardFile)

            with open(mainFile, 'w') as f:
                json.dump(data, f)
            if not isFileWorldWritable(mainFile):
                os.chmod(mainFile, 0o777)  # file may be amended by another process

        if filesTouched:
            self.log.info(f"Uploading {len(filesTouched)} metadata files")
            for file in filesTouched:
                self.uploader.googleUpload(self.channel, file, isLiveFile=True)
        return

    def getSidecarFilename(self, dayObs):
        """Get the name of the metadata sidecar file for the dayObs.

        Returns
        -------
        dayObs : `int`
            The dayObs.
        """
        return os.path.join(self.outputRoot, f'dayObs_{dayObs}.json')

    def callback(self, dataId):
        """Method called on each new dataId as it is found in the repo.

        Add the metadata to the sidecar for the dataId and upload.
        """
        try:
            self.log.info(f'Getting metadata for {dataId}')
            self.writeShardForDataId(dataId)
            self.mergeShardsAndUpload()  # updates all shards everywhere

        except Exception as e:
            if self.doRaise:
                raise RuntimeError(f"Error processing {dataId}") from e
            self.log.warning(f"Skipped creating sidecar metadata for {dataId} because {repr(e)}")
            return None

    def run(self):
        """Run continuously, calling the callback method on the latest dataId.
        """
        self.watcher.run(self.callback)


class Uploader():
    """Class for handling uploads to the Google cloud storage bucket.
    """
    HEARTBEAT_PREFIX = "heartbeats"

    def __init__(self, bucketName):
        if not HAS_GOOGLE_STORAGE:
            from lsst.summit.utils.utils import GOOGLE_CLOUD_MISSING_MSG
            raise RuntimeError(GOOGLE_CLOUD_MISSING_MSG)
        self.client = storage.Client()
        self.bucket = self.client.get_bucket(bucketName)
        self.log = _LOG.getChild("googleUploader")

    def uploadHeartbeat(self, channel, flatlinePeriod):
        """Upload a heartbeat for the specified channel to the bucket.

        Parameters
        ----------
        channel : `str`
            The channel name.
        flatlinePeriod : `float`
            The period after which to consider the channel dead.

        Returns
        -------
        success : `bool`
            Did the upload succeed?
        """
        filename = "/".join([self.HEARTBEAT_PREFIX, channel]) + ".json"

        currTime = int(time.time())
        nextExpected = currTime + flatlinePeriod

        heartbeatJsonDict = {
            "channel": channel,
            "currTime": currTime,
            "nextExpected": nextExpected,
            "errors": {}
        }
        heartbeatJson = json.dumps(heartbeatJsonDict)

        blob = self.bucket.blob("/".join([filename]))
        blob.cache_control = 'no-store'  # must set before upload

        # heartbeat retry strategy
        modified_retry = DEFAULT_RETRY.with_deadline(0.6)  # single retry here
        modified_retry = modified_retry.with_delay(initial=0.5, multiplier=1.2, maximum=2)

        try:
            blob.upload_from_string(heartbeatJson, retry=modified_retry)
            self.log.debug(f'Uploaded heartbeat to channel {channel} with datetime {currTime}')
            return True
        except Exception:
            return False

    def uploadPerSeqNumPlot(self,
                            channel,
                            dayObsInt,
                            seqNumInt,
                            filename,
                            isLiveFile=False,
                            isLargeFile=False,
                            ):
        """Upload a per-dayObs/seqNum plot to the bucket.

        Parameters
        ----------
        channel : `str`
            The RubinTV channel to upload to.
        dayObsInt : `int`
            The dayObs of the plot.
        seqNumInt : `int`
            The seqNum of the plot.
        filename : `str`
            The full path and filename of the file to upload.
        isLiveFile : `bool`, optional
            The file is being updated constantly, and so caching should be
        disabled.
        isLargeFile : `bool`, optional
            The file is large, so add a longer timeout to the upload.

        Raises
        ------
        ValueError
            Raised if the specified channel is not in the list of existing
            channels as specified in CHANNELS
        RuntimeError
            Raised if the Google cloud storage is not installed/importable.
        """
        if channel not in CHANNELS:
            raise ValueError(f"Error: {channel} not in {CHANNELS}")

        dayObsStr = dayObsIntToString(dayObsInt)
        # TODO: sort out this prefix nonsense as part of the plot organization
        # fixup in the new year?
        plotPrefix = channel.replace('_', '-')

        uploadAs = f"{channel}/{plotPrefix}_dayObs_{dayObsStr}_seqNum_{seqNumInt}.png"

        blob = self.bucket.blob(uploadAs)
        if isLiveFile:
            blob.cache_control = 'no-store'

        # general retry strategy
        # still quite gentle as the catchup service will fill in gaps
        # and we don't want to hold up new images landing
        timeout = 1000 if isLargeFile else 60  # default is 60s
        deadline = timeout if isLargeFile else 2.0
        modified_retry = DEFAULT_RETRY.with_deadline(deadline)  # in seconds
        modified_retry = modified_retry.with_delay(initial=.5, multiplier=1.2, maximum=2)
        try:
            blob.upload_from_filename(filename, retry=modified_retry, timeout=timeout)
            self.log.info(f'Uploaded {filename} to {uploadAs}')
        except Exception as e:
            self.log.warning(f"Failed to upload {uploadAs} to {channel} because {repr(e)}")
            return None

        return blob

    def googleUpload(self, channel, sourceFilename,
                     uploadAsFilename=None,
                     isLiveFile=False,
                     isLargeFile=False,
                     ):
        """Upload a file to the RubinTV Google cloud storage bucket.

        Parameters
        ----------
        channel : `str`
            The RubinTV channel to upload to.
        sourceFilename : `str`
            The full path and filename of the file to upload.
        uploadAsFilename : `str`, optional
            Optionally rename the file to this upon upload.
        isLiveFile : `bool`, optional
            The file is being updated constantly, and so caching should be
        disabled.
        isLargeFile : `bool`, optional
            The file is large, so add a longer timeout to the upload.

        Raises
        ------
        ValueError
            Raised if the specified channel is not in the list of existing
            channels as specified in CHANNELS
        RuntimeError
            Raised if the Google cloud storage is not installed/importable.
        """
        if channel not in CHANNELS:
            raise ValueError(f"Error: {channel} not in {CHANNELS}")

        if not uploadAsFilename:
            uploadAsFilename = os.path.basename(sourceFilename)
        finalName = "/".join([channel, uploadAsFilename])

        blob = self.bucket.blob(finalName)
        if isLiveFile:
            blob.cache_control = 'no-store'

        # general retry strategy
        # still quite gentle as the catchup service will fill in gaps
        # and we don't want to hold up new images landing
        timeout = 1000 if isLargeFile else 60  # default is 60s
        deadline = timeout if isLargeFile else 2.0
        modified_retry = DEFAULT_RETRY.with_deadline(deadline)  # in seconds
        modified_retry = modified_retry.with_delay(initial=.5, multiplier=1.2, maximum=2)
        try:
            blob.upload_from_filename(sourceFilename, retry=modified_retry, timeout=timeout)
            self.log.info(f'Uploaded {sourceFilename} to {finalName}')
        except Exception as e:
            self.log.warning(f"Failed to upload {finalName} to {channel} because {repr(e)}")
            return None

        return blob


class Heartbeater():
    """A class for uploading heartbeats to the GCS bucket.

    Call ``beater.beat()`` as often as you like. Files will only be uploaded
    once ``self.uploadPeriod`` has elapsed, or if ``ensure=True`` when calling
    beat.

    Parameters
    ----------
    handle : `str`
        The name of the channel to which the heartbeat corresponds.
    bucketName : `str`
        The name of the bucket to upload the heartbeats to.
    uploadPeriod : `float`
        The time, in seconds, which must have passed since the last successful
        heartbeat for a new upload to be undertaken.
    flatlinePeriod : `float`
        If a new heartbeat is not received before the flatlinePeriod elapses,
        in seconds, the channel will be considered down.
    """
    def __init__(self, handle, bucketName, uploadPeriod, flatlinePeriod):
        self.handle = handle
        self.uploadPeriod = uploadPeriod
        self.flatlinePeriod = flatlinePeriod

        self.lastUpload = -1
        self.uploader = Uploader(bucketName)

    def beat(self, ensure=False, customFlatlinePeriod=None):
        """Upload the heartbeat if enough time has passed or ensure=True.

        customFlatlinePeriod implies ensure, as long forecasts should always be
        uploaded.

        Parameters
        ----------
        ensure : `str`
            Ensure that the heartbeat is uploaded, even if one has been sent
            within the last ``uploadPeriod``.
        customFlatlinePeriod : `float`
            Upload with a different flatline period. Use before starting long
            running jobs.
        """
        forecast = self.flatlinePeriod if not customFlatlinePeriod else customFlatlinePeriod

        now = time.time()
        elapsed = now - self.lastUpload
        if (elapsed >= self.uploadPeriod) or ensure or customFlatlinePeriod:
            if self.uploader.uploadHeartbeat(self.handle, forecast):  # returns True on successful upload
                self.lastUpload = now  # only reset this if the upload was successful


class CalibrateCcdRunner():
    """Class for running CharacterizeImageTask and CalibrateTasks on images.

    Runs these tasks and writes shards with various measured quantities for
    upload to the table.
    """
    def __init__(self, location, *, doRaise=False, embargo=False):
        self.config = LocationConfig(location)
        self.dataProduct = 'quickLookExp'
        self.uploader = Uploader(self.config.bucketName)
        # writeable true is required to define visits
        self.butler = butlerUtils.makeDefaultLatissButler(embargo=embargo, writeable=True)
        self.log = _LOG.getChild("calibrateCcdRunner")
        self.channel = 'auxtel_calibrateCcd'
        self.watcher = FileWatcher(location, self.dataProduct, self.channel)
        self.doRaise = doRaise
        self.shardsDir = self.config.metadataShardPath
        # TODO DM-37272 need to get the collection name from a central place
        self.outputRunName = "LATISS/runs/quickLook/1"

        config = CharacterizeImageConfig()
        basicConfig = CharacterizeImageConfig()
        obs_lsst = getPackageDir("obs_lsst")
        config.load(os.path.join(obs_lsst, "config", "characterizeImage.py"))
        config.load(os.path.join(obs_lsst, "config", "latiss", "characterizeImage.py"))
        config.measurement = basicConfig.measurement

        config.doApCorr = False
        config.doDeblend = False
        self.charImage = CharacterizeImageTask(config=config)

        config = CalibrateConfig()
        basicConfig = CalibrateConfig()
        config.load(os.path.join(obs_lsst, "config", "calibrate.py"))
        config.load(os.path.join(obs_lsst, "config", "latiss", "calibrate.py"))
        config.measurement = basicConfig.measurement

        # TODO DM-37426 add some more overrides to speed up runtime
        config.doApCorr = False
        config.doDeblend = False

        self.calibrate = CalibrateTask(config=config, icSourceSchema=self.charImage.schema)

    def _getRefObjLoader(self, refcatName, dataId, config):
        """Construct a referenceObjectLoader for a given refcat

        Parameters
        ----------
        refcatName : `str`
            Name of the reference catalog to load
        dataId : `dict`
            DataId to determine bounding box of sources to load
        config : `lsst.meas.algorithms.LoadReferenceObjectsConfig`
            Configuration for the reference object loader

        Returns
        -------
        loader : `lsst.meas.algorithms.ReferenceObjectLoader`
        """
        refs = self.butler.registry.queryDatasets(refcatName, dataId=dataId).expanded()
        # generator not guaranteed to yield in the same order every iteration
        # therefore critical to materialize a list before iterating twice
        refs = list(refs)
        handles = [dafButler.DeferredDatasetHandle(butler=self.butler, ref=ref, parameters=None)
                   for ref in refs]
        dataIds = [ref.dataId for ref in refs]

        loader = ReferenceObjectLoader(
            dataIds,
            handles,
            name=refcatName,
            log=self.log,
            config=config
        )
        return loader

    def callback(self, dataId):
        """Method called on each new dataId as it is found in the repo.

        Runs on the quickLookExp and writes shards with various measured
        quantities, as calculated by the CharacterizeImageTask and
        CalibrateTask.
        """
        try:
            tStart = time.time()

            self.log.info(f'Running Image Characterization for {dataId}')
            exp = _waitForDataProduct(self.butler, self.dataProduct, dataId, self.log)

            if not exp:
                raise RuntimeError(f'Failed to get {self.dataProduct} for {dataId}')

            # TODO DM-37427 dispersed images do not have a filter and fail
            if isDispersedExp(exp):
                self.log.info(f'Skipping dispersed image: {dataId}')
                return

            visitDataId = self.getVisitDataId(dataId)
            if not visitDataId:
                self.defineVisit(dataId)
                visitDataId = self.getVisitDataId(dataId)

            loader = self._getRefObjLoader(self.calibrate.config.connections.astromRefCat, visitDataId,
                                           config=self.calibrate.config.astromRefObjLoader)
            self.calibrate.astrometry.setRefObjLoader(loader)
            loader = self._getRefObjLoader(self.calibrate.config.connections.photoRefCat, visitDataId,
                                           config=self.calibrate.config.photoRefObjLoader)
            self.calibrate.photoCal.match.setRefObjLoader(loader)

            charRes = self.charImage.run(exp)
            tCharacterize = time.time()
            self.log.info(f"Ran characterizeImageTask in {tCharacterize-tStart:.2f} seconds")

            nSources = len(charRes.sourceCat)
            dayObs = butlerUtils.getDayObs(dataId)
            seqNum = butlerUtils.getSeqNum(dataId)
            outputDict = {"50-sigma source count": nSources}
            # flag as measured to color the cells in the table
            labels = {"_" + k: "measured" for k in outputDict.keys()}
            outputDict.update(labels)

            mdDict = {seqNum: outputDict}
            writeMetadataShard(self.shardsDir, dayObs, mdDict)

            calibrateRes = self.calibrate.run(charRes.exposure,
                                              background=charRes.background,
                                              icSourceCat=charRes.sourceCat)
            tCalibrate = time.time()
            self.log.info(f"Ran calibrateTask in {tCalibrate-tCharacterize:.2f} seconds")

            summaryStats = calibrateRes.outputExposure.getInfo().getSummaryStats()
            pixToArcseconds = calibrateRes.outputExposure.getWcs().getPixelScale().asArcseconds()
            SIGMA2FWHM = np.sqrt(8 * np.log(2))
            e1 = (summaryStats.psfIxx - summaryStats.psfIyy) / (summaryStats.psfIxx + summaryStats.psfIyy)
            e2 = 2*summaryStats.psfIxy / (summaryStats.psfIxx + summaryStats.psfIyy)

            outputDict = {
                '5-sigma source count': len(calibrateRes.outputCat),
                'PSF FWHM': summaryStats.psfSigma * SIGMA2FWHM * pixToArcseconds,
                'PSF e1': e1,
                'PSF e2': e2,
                'Sky mean': summaryStats.skyBg,
                'Sky RMS': summaryStats.skyNoise,
                'Variance plane mean': summaryStats.meanVar,
                'PSF star count': summaryStats.nPsfStar,
                'Astrometric bias': summaryStats.astromOffsetMean,
                'Astrometric scatter': summaryStats.astromOffsetStd,
                'Zeropoint': summaryStats.zeroPoint
            }

            # flag all these as measured items to color the cell
            labels = {"_" + k: "measured" for k in outputDict.keys()}
            outputDict.update(labels)

            mdDict = {seqNum: outputDict}
            writeMetadataShard(self.shardsDir, dayObs, mdDict)
            self.log.info(f'Wrote metadata shard. Putting calexp for {dataId}')
            self.clobber(calibrateRes.outputExposure, "calexp", dataId)
            tFinal = time.time()
            self.log.info(f"Ran characterizeImage and calibrate in {tFinal-tStart:.2f} seconds")

        except Exception as e:
            if self.doRaise:
                raise RuntimeError(f"Error processing {dataId}") from e
            self.log.warning(f"Did not finish calibration of {dataId} because {repr(e)}")
            return None

    def defineVisit(self, dataId):
        """Define a visit in the registry, given a dataId.

        Note that this takes about 9ms regardless of whether it exists, so it
        is no quicker to check than just run the define call.

        NB: butler must be writeable for this to work.

        Parameters
        ----------
        dataId : `dict`
            DataId to define a visit for
        """
        instr = Instrument.from_string(self.butler.registry.defaults.dataId['instrument'],
                                       self.butler.registry)
        config = DefineVisitsConfig()
        instr.applyConfigOverrides(DefineVisitsTask._DefaultName, config)

        task = DefineVisitsTask(config=config, butler=self.butler)

        task.run([butlerUtils.getExpIdFromDayObsSeqNum(self.butler, dataId)],
                 collections=self.butler.collections)

    def getVisitDataId(self, dataId):
        """Lookup visitId for a dataId containing an exposureId or

        other uniquely identifying keys such as dayObs and seqNum.

        Parameters
        ----------
        dataId : `dict`
            DataId to look up the visitId

        Returns
        -------
        visitDataId : lsst.daf.butler.DataCoordinate
            Data Id containing a visitId
        """
        expId = butlerUtils.getExpIdFromDayObsSeqNum(self.butler, dataId)
        visitDataIds = self.butler.registry.queryDataIds(["visit", "detector"], dataId=expId)
        visitDataIds = list(set(visitDataIds))
        if len(visitDataIds) == 1:
            visitDataId = visitDataIds[0]
            return visitDataId
        else:
            self.log.warning(f"Failed to find visitId for {dataId}, got {visitDataIds}. Do you need to run"
                             " define-visits?")
            return None

    def clobber(self, object, datasetType, dataId):
        """Put object in the butler.

        If there is one already there, remove it beforehand

        Parameters
        ----------
        object : `object`
            Any object to put in the butler
        datasetType : `str`
            Dataset type name to put it as
        dataId : `dict`
            DataId to put the object at
        """
        if not (visitDataId := self.getVisitDataId(dataId)):
            self.log.warning(f'Skipped butler.put of {datasetType} for {dataId} due to lack of visitId.'
                             " Do you need to run define-visits?")
            return

        self.butler.registry.registerRun(self.outputRunName)
        if butlerUtils.datasetExists(self.butler, datasetType, visitDataId):
            self.log.warning(f'Overwriting existing {datasetType} for {dataId}')
            dRef = self.butler.registry.findDataset(datasetType, visitDataId)
            self.butler.pruneDatasets([dRef], disassociate=True, unstore=True, purge=True)
        self.butler.put(object, datasetType, dataId=visitDataId, run=self.outputRunName)
        self.log.info(f'Put {datasetType} for {dataId} with visitId: {visitDataId}')

    def run(self):
        """Run continuously, calling the callback method on the latest dataId.
        """
        self.watcher.run(self.callback)
