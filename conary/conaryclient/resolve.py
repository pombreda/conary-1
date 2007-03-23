#
# Copyright (c) 2004-2006 rPath, Inc.
#
# This program is distributed under the terms of the Common Public License,
# version 1.0. A copy of this license should have been distributed with this
# source file in a file called LICENSE. If it is not present, the license
# is always available at http://www.opensource.org/licenses/cpl.php.
#
# This program is distributed in the hope that it will be useful, but
# without any warranty; without even the implied warranty of merchantability
# or fitness for a particular purpose. See the Common Public License for
# full details.

import itertools
import re

from conary import errors
from conary.lib import log
from conary.repository import trovesource
from conary.repository import errors as repoerrors
from conary.repository.resolvemethod import *
from conary.deps import deps

class DependencySolver(object):

    def __init__(self, client, cfg, repos, db):
        self.client = client
        self.cfg = cfg
        self.repos = repos
        self.db = db

    def _findCriticalJobInfo(self, jobSet, updateSettings):
        if updateSettings is None:
            return [], [], False
        criticalJobs = updateSettings.findCriticalJobs(jobSet)
        finalJobs = updateSettings.findFinalJobs(jobSet)
        return criticalJobs, finalJobs, updateSettings.isCriticalOnlyUpdate()

    def resolveDependencies(self, uJob, jobSet, split = False,
                            resolveDeps = True, useRepos = True,
                            resolveSource = None, keepRequired = True,
                            criticalUpdateInfo = None):
        """
            Determine and possibly resolve dependency problems.
            @param uJob: update job we are resolving dependencies for
            @type uJob: local.database.UpdateJob
            @param jobSet: jobs that are to be applied
            @type jobSet: list of job tuples
            @param split: if True, find an ordering for the jobs in 
            jobSet plus any resolved jobs. 
            @type split: bool
            @param resolveDeps: If True, try to resolve any dependency problems
            by modifying the given job.
            @type resolveDeps: bool
            @param useRepos: If True, search for dependency solutions in the 
            repository after searching the update job search source.
            @param keepRequired: If True, the resolver will attempt to remove
            erase jobs from the job set to resolve dependency problems
            created by the erasure. If False, those problems will be reported
            instead.
            @type keepRequired: bool
        """
        troveSource = uJob.getSearchSource()
        if useRepos:
            troveSource = trovesource.stack(troveSource, self.repos)

        ineligible = set()

        (depList, cannotResolve, changeSetList, keepList, ineligible,
         criticalUpdates) = self.checkDeps(uJob, jobSet, troveSource,
                                       findOrdering = split,
                                       resolveDeps = resolveDeps,
                                       ineligible=ineligible,
                                       keepRequired = keepRequired,
                                       criticalUpdateInfo = criticalUpdateInfo)


        if not resolveDeps:
            # we're not supposed to resolve deps here; just skip the
            # rest of this
            depList = []
            cannotResolve = []

        if resolveSource is None:
            resolveSource = DepResolutionByLabelPath(self.cfg, self.db,
                                             self.cfg.installLabelPath,
                                             searchMethod=RESOLVE_LEAVES_FIRST)

        resolveSource.setTroveSource(troveSource)

        suggMap = {}

        while resolveSource.prepareForResolution(depList):

            sugg = resolveSource.resolveDependencies()
            newTroves = resolveSource.filterSuggestions(depList, sugg, suggMap)

            if not newTroves:
                continue

            changedJob = self.addUpdates(newTroves, uJob, jobSet, ineligible,
                                         keepList, troveSource)
            if not changedJob:
                continue

            (depList, cannotResolve, changeSetList, newKeepList,
             ineligible, criticalUpdates) =  self.checkDeps(uJob, jobSet,
                                       uJob.getTroveSource(),
                                       findOrdering = True,
                                       resolveDeps = True,
                                       ineligible = ineligible,
                                       keepRequired = keepRequired,
                                       criticalUpdateInfo = criticalUpdateInfo)
            keepList.extend(newKeepList)

        if criticalUpdateInfo is None:
            # backwards compatibility with conary v. 1.0.30/1.1.3 and earlier
            return (depList, suggMap, cannotResolve, changeSetList, keepList)
        return (depList, suggMap, cannotResolve, changeSetList, keepList,
                criticalUpdates)

    def addUpdates(self, troves, uJob, jobSet, ineligible, keepList, 
                    troveSource):
        """
        Add the given dep resolution solutions to the current jobSet.
        """
        # We found good suggestions, merge in those troves. Items
        # which are being removed by the current job cannot be 
        # removed again.
        beingRemoved = set((x[0], x[1][0], x[1][1]) for x in
                            jobSet if x[1][0] is not None )
        beingInstalled = set((x[0], x[2][0], x[2][1]) for x in
                              jobSet if x[2][0] is not None )


        # add in foo if we are adding foo:lib.  That way 'conary
        # erase foo' will work as expected.
        if self.cfg.autoResolvePackages:
            packageJobs = self.addPackagesForComponents(troves,
                                                        troveSource,
                                                        beingInstalled)
            troves.update(packageJobs)

        newJob = self.client._updateChangeSet(troves, uJob,
                                              keepExisting = False,
                                              recurse = False,
                                              ineligible = beingRemoved,
                                              checkPrimaryPins = True)
        assert(not (newJob & jobSet))
        newJob = self.filterCrossBranchResolutions(newJob, troveSource)
        if not newJob:
            # we had potential solutions, but they would have
            # required implicitly switching the branch of a trove
            # on a user, and we don't do that.
            return False

        jobSet.update(newJob)
        return True

    def checkDeps(self, uJob, jobSet, trvSrc, findOrdering,
                  resolveDeps, ineligible, keepRequired = True,
                  criticalUpdateInfo = None):
        """
            Given a jobSet, use its dependencies to determine an
            ordering, resolve problems with jobs that have difficult
            dependency problems immediately,
            and determine any missing dependencies.
        """

        keepList = []

        while True:
            linkedJobs = self.client._findOverlappingJobs(jobSet,
                                                          uJob.getTroveSource())
            criticalJobs, finalJobs, criticalOnly = self._findCriticalJobInfo(
                                                         jobSet,
                                                         criticalUpdateInfo)
            (depList, cannotResolve, changeSetList, criticalUpdates) = \
                            self.db.depCheck(jobSet, uJob.getTroveSource(),
                                             findOrdering = findOrdering,
                                             linkedJobs = linkedJobs,
                                             criticalJobs = criticalJobs,
                                             finalJobs = finalJobs,
                                             criticalOnly = criticalOnly)

            if not resolveDeps or not cannotResolve:
                break
            # We have troves that are in the state cannotResolve:
            # This means that they are troves that are needed by
            # something else that we are trying to erase.

            # We attempt to solve them here in this inner loop because
            # they are more difficult to resolve, and resolving them may
            # affect resolving normal missing dependencies.

            changeMade = False

            # first: attempt to update packages that dependended on the missing
            # package.
            log.debug('Update breaks dependencies!')
            for reqInfo, depSet, provInfo in sorted(cannotResolve,
                                                    key=lambda x:x[0][0]):
                depSet = '\n     '.join(str(depSet).split('\n'))
                msg = (
                    'Broken dependency: (dep needed by the system but being removed):\n'
                     '   %s=%s\n'
                     '   Requires:\n'
                     '     %s\n'
                     '   Provided by removed or updated packages: %s')
                args = (reqInfo[0], reqInfo[1].trailingRevision(), depSet, 
                        ', '.join(x[0] for x in provInfo))
                log.debug(msg, *args)
            if self.cfg.resolveLevel > 1:
                cannotResolve, newJobSet = self.resolveEraseByUpdating(
                                                                trvSrc,
                                                                cannotResolve,
                                                                uJob, jobSet,
                                                                ineligible)
                if newJobSet:
                    jobSet |= newJobSet
                    changeMade = True

            if not changeMade and cannotResolve and keepRequired:
                # second: attempt to keep packages that were being erased
                cannotResolve, newKeepList = self.resolveEraseByKeeping(
                                                         trvSrc,
                                                         cannotResolve,
                                                         uJob, jobSet)
                if newKeepList:
                    changeMade = True
                    keepList += newKeepList

            if not changeMade:
                break

        return (depList, cannotResolve, changeSetList, keepList, ineligible,
                criticalUpdates)

    def resolveEraseByUpdating(self, trvSrc, cannotResolve, uJob, jobSet, 
                               ineligible):
        """
            Attempt to resolve broken erase dependencies by updating the 
            package that has the dependency on the trove that is being 
            erased.
            @param ineligible: Ineligible troves are troves that were
            involved in resolveEraseByUpdating before.
        """

        oldIdx = {}
        newIdx = {}
        for job in jobSet:
            if job[1][0] is not None:
                oldIdx[(job[0], job[1][0], job[1][1])] = job
            if job[2][0] is not None:
                newIdx[(job[0], job[2][0], job[2][1])] = job

        potentialUpdateList = []

        for resolveInfo in cannotResolve:
            (reqInfo, depSet, provInfoList) = resolveInfo
            # reqInfo = the trove the requires the dependency
            # provInfo = the troves that provide the dependency

            if reqInfo in newIdx:
                # The thing with the requirement is something we asked 
                # to be installed - don't try to update it again!
                continue

            found = False
            for provInfo in provInfoList:
                if provInfo in ineligible:
                    # The trove that we erased due to an earlier 
                    # resolveEraseByUpdating was required by other troves!
                    # We don't allow this process to recurse to avoid
                    # accidentally updating your entire system due to 
                    # a glibc update request, e.g.
                    found = True
                    break
            if found:
                continue

            for provInfo in provInfoList:
                if provInfo not in oldIdx:
                    continue

                job = oldIdx[provInfo]

                if not job[2][0]:
                    # this was an erase, not an update, we don't attempt
                    # use this method on erases.
                    continue

                # we're trying to update this package.
                # don't allow recursion to take place, where things that
                # require the reqInfo are updated.
                ineligible.add(reqInfo)
                potentialUpdateList.append((reqInfo, depSet, provInfoList))
                break

        if not potentialUpdateList:
            return cannotResolve, set()

        log.debug('Attempting to update the following packages in order to '
                  'remove their dependency on something being erased:\n   %s', ('\n   '.join(sorted(x[0][0].split(':')[0] for x in potentialUpdateList))))

        # attempt to update the _package_ that has the requirement that
        # is now erased.
        updateJobs = [ (x[0][0].split(':')[0],
                        (None, None), (None, None), True)
                        for x in potentialUpdateList ]

        try:
            newJob, suggMap = self.client.updateChangeSet(updateJobs,
                                                     keepExisting=False,
                                                     resolveDeps=False,
                                                     split=True)
            newJobSet = newJob.getJobs()

            # ignore updates where updating this trove would update
            # to something that's already in the jobSet
            jobs = ((x for x in jobSet
                     if (x[0], x[2][0], x[2][1]) not in newIdx)
                    for jobSet in newJobSet)
            newJobSet = set(itertools.chain(*jobs))

            # We were able to update some troves that required troves
            # that were in the cannot resolve list.
            # Calculate the new state of dependencies, and make sure
            # there is actually a change

            uJob.getTroveSource().merge(newJob.getTroveSource())

            (depList, newCannotResolve, changeSetList, criticalUpdates) = \
                    self.db.depCheck(jobSet | newJobSet,
                                     uJob.getTroveSource(),
                                     findOrdering = False,
                                     criticalJobs=[])
            if cannotResolve != newCannotResolve:
                cannotResolve = newCannotResolve
            else:
                newJobSet = set()
        except (errors.ClientError, errors.TroveNotFound):
            newJobSet = set()

        if newJobSet:
            log.debug('updated %s troves:\n   %s', len(newJobSet), 
                       '\n   '.join(sorted('%s=%s/%s[%s]' % (x[0], x[2][0].branch().label(), x[2][0].trailingRevision(), x[2][1]) for x in newJobSet if x[2][0])))

        return cannotResolve, newJobSet

    def resolveEraseByKeeping(self, trvSrc, cannotResolve, uJob, jobSet):
        oldIdx = {}
        for job in jobSet:
            if job[1][0] is not None:
                oldIdx[(job[0], job[1][0], job[1][1])] = job

        restoreSet = set()

        keepList = []
        for resolveInfo in cannotResolve[:]:
            (reqInfo, depSet, provInfoList) = resolveInfo
            # Modify/remove non-primary jobs that cause
            # irreconcilable dependency problems.
            for provInfo in provInfoList:
                if provInfo not in oldIdx: continue

                job = oldIdx[provInfo]
                if job in restoreSet:
                    break

                # if erasing this was a primary job, don't break
                # it up
                if (job[0], job[1], (None, None), False) in \
                        uJob.getPrimaryJobs():
                    continue

                if job[2][0] is None:
                    # this was an erasure implied by package changes;
                    # leaving it in place won't hurt anything
                    keepList.append((job, depSet, reqInfo))
                    cannotResolve.remove(resolveInfo)
                    restoreSet.add(job)
                    depSet = '\n               '.join(str(depSet).split('\n'))
                    msg = ('Resolved (undoing erasure)\n'
                           '    %s=%s[%s]'
                           '    Required: %s'
                           '    Keeping: %s=%s[%s]')
                    args = (reqInfo[0], reqInfo[1].trailingRevision(), reqInfo[2], depSet, job[0], job[1][0].trailingRevision(), job[1][1])
                    log.debug(msg, *args)
                    break

                oldTrv = self.db.getTrove(withFiles = False,
                                          *provInfo)
                newTrv = trvSrc.getTrove(job[0], job[2][0], job[2][1],
                                         withFiles = False)

                if oldTrv.compatibleWith(newTrv):
                    restoreSet.add(job)
                    keepList.append((job, depSet, reqInfo))
                    cannotResolve.remove(resolveInfo)
                    msg = ('Resolved (installing side-by-side)\n'
                           '    %s=%s[%s]'
                           '    Required: %s'
                           '    Keeping: %s=%s[%s]')
                    args = (reqInfo[0], reqInfo[1].trailingRevision(),
                            reqInfo[2], depSet, 
                            job[0], job[1][0].trailingRevision(), job[1][1])
                    log.debug(msg, *args)


                    break

        if self.cfg.autoResolvePackages:
           # if we're keeping any components, keep the package as well.
           jobsByOld = dict(((x[0], x[1]), x) for x in jobSet 
                            if ':' not in x[0])
           for job in list(restoreSet):
               if ':' in job[0]:
                   pkgJob = jobsByOld.get((job[0].split(':')[0], job[1]), None)
                   if pkgJob:
                       restoreSet.add(pkgJob)

        for job in restoreSet:
            jobSet.remove(job)
            if job[2][0] is not None:
                # if there was an install portion of the job,
                # retain it
                jobSet.add((job[0], (None, None), job[2], False))

        return (cannotResolve, keepList)

    def addPackagesForComponents(self, troves, troveSource, beingInstalled):
        packages = {}
        for job in troves:
            if ':' in job[0]:
                pkgName = job[0].split(':', 1)[0]
                pkgInfo = (pkgName, job[2][0], job[2][1])
                if pkgInfo in beingInstalled:
                    continue
                packages[pkgName, job[2][0], job[2][1]] = (pkgName, job[1], job[2], True)
        toCheck = list(packages)
        hasTroves = troveSource.hasTroves(toCheck)
        if isinstance(hasTroves, list):
            hasTroves = dict(itertools.izip(toCheck, hasTroves))
        packageJobs = [x[1] for x in packages.iteritems() if hasTroves[x[0]]]
        return packageJobs

    def filterCrossBranchResolutions(self, jobSet, troveSource):
        # We can't resolve deps in a way that would cause conary to
        # switch the branch of a trove.
        crossBranchJobs = [ x for x in jobSet
                            if (x[1][0] and
                                x[1][0].branch() != x[2][0].branch()) ]
        if crossBranchJobs:
            jobSet.difference_update(crossBranchJobs)
            oldTroves = self.db.getTroves(
                  [ (x[0], x[1][0], x[1][1]) for x in crossBranchJobs ],
                  withFiles = False)
            newTroves = troveSource.getTroves(
                  [ (x[0], x[2][0], x[2][1]) for x in crossBranchJobs ],
                  withFiles = False)
            for job, oldTrv, newTrv in itertools.izip(crossBranchJobs,
                                                      oldTroves,
                                                      newTroves):
                if oldTrv.compatibleWith(newTrv):
                    jobSet.add((job[0], (None, None), job[2], False))
        return jobSet
