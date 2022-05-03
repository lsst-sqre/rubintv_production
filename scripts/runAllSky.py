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

import logging
import sys
from lsst.rubintv.production.allSky import AllSkyMovieChannel
from lsst.rubintv.production.utils import checkRubinTvExternalPackages

rootDataPath = '/lsstdata/offline/allsky/storage/'  # XXX change this for the summit k8s path
outputRoot = '/home/mfl/allskycamdev/'  # XXX change this to /scratch/rubintv

checkRubinTvExternalPackages()
# don't use setupLogging() here to allow for more fine-grained control of
# log levels, at least at first
logging.basicConfig(level=logging.DEBUG, stream=sys.stdout)  # XXX turn this on

print('Running allSky channel...')
allSky = AllSkyMovieChannel(rootDataPath, outputRoot)
allSky.run()
