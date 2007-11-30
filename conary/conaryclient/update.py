#
# Copyright (c) 2004-2007 rPath, Inc.
#
# This program is distributed under the terms of the Common Public License,
# version 1.0. A copy of this license should have been distributed with this
# source file in a file called LICENSE. If it is not present, the license
# is always available at http://www.rpath.com/permanent/licenses/CPL-1.0.
#
# This program is distributed in the hope that it will be useful, but
# without any warranty; without even the implied warranty of merchantability
# or fitness for a particular purpose. See the Common Public License for
# full details.

import itertools
import re
import os
import tempfile
import traceback
import sys

from conary import conarycfg, constants
from conary.callbacks import UpdateCallback
from conary.conaryclient import cmdline, resolve
from conary.deps import deps
from conary.errors import ClientError, ConaryError, InternalConaryError, MissingTrovesError
from conary.lib import log, util
from conary.local import database
from conary.repository import changeset, trovesource, searchsource
from conary.repository.errors import TroveMissing, OpenError
from conary import trove, versions

class CriticalUpdateInfo(object):
    """
        Defines update settings regarding critical jobs - those required
        to go first and those required to go last.
    """

    criticalTroveRegexps = []
    finalTroveRegexps = []

    def _match(self, regexpList, jobList):
        l = []
        for job in jobList:
            for regexp in regexpList:
                if job[2][0] and re.match(regexp, job[0]):
                    l.append(job)
        return l

    def findCriticalJobs(self, jobList):
        return self._match(self.criticalTroveRegexps, jobList)

    def setFinalTroveRegexps(self, regexpList):
        self.finalTroveRegexps = regexpList

    def setCriticalTroveRegexps(self, regexpList):
        self.criticalTroveRegexps = regexpList

    def findFinalJobs(self, jobList):
        return self._match(self.finalTroveRegexps, jobList)

    def addChangeSet(self, cs, includesFileContents):
        """ Store a changeset usable by the update determination code """
        self.changeSetList.append((cs, includesFileContents))

    def iterChangeSets(self):
        return iter(self.changeSetList)

    def isCriticalOnlyUpdate(self):
        """ Returns true if this job should just apply critical updates """
        return self.criticalOnly

    def __init__(self, criticalOnly=False):
        self.criticalOnly = criticalOnly
        self.changeSetList = []


class UpdateChangeSet(changeset.ReadOnlyChangeSet):

    def merge(self, cs, src = None):
        changeset.ReadOnlyChangeSet.merge(self, cs)
        if isinstance(cs, UpdateChangeSet):
            self.contents += cs.contents
        else:
            self.contents.append(src)
        self.empty = False

    def __init__(self, *args):
        changeset.ReadOnlyChangeSet.__init__(self, *args)
        self.contents = []
        self.empty = True

class ClientUpdate:

    @staticmethod
    def revertJournal(cfg):
        try:
            database.Database.revertJournal(cfg.root, cfg.dbPath)
        except database.OpenError, e:
            log.error(str(e))

    def __init__(self, callback=None):
        self.updateCallback = None
        self.setUpdateCallback(callback)
        self.lzCache = util.LazyFileCache()

    def getUpdateCallback(self):
        return self.updateCallback

    def setUpdateCallback(self, callback):
        assert(callback is None or isinstance(callback, UpdateCallback))
        self.updateCallback = callback
        return self

    def _resolveDependencies(self, uJob, jobSet, split = False,
                             resolveDeps = True, useRepos = True,
                             resolveSource = None, keepRequired = True,
                             criticalUpdateInfo = None):
        return self.resolver.resolveDependencies(uJob, jobSet, split=split,
                                     resolveDeps=resolveDeps,
                                     useRepos=useRepos,
                                     resolveSource=resolveSource,
                                     keepRequired = keepRequired,
                                     criticalUpdateInfo = criticalUpdateInfo)

    def _processRedirects(self, csSource, uJob, jobSet, transitiveClosure,
                          recurse):
        """
        Looks for redirects in the change set, and returns a list of troves
        which need to be included in the update.  This returns redirectHack,
        which maps targets of redirections to the sources of those 
        redirections.
        """

        troveSet = {}
        redirectHack = {}

        jobsToRemove = []
        jobsToAdd = []
        transitiveClosureToRemove = []

        # We don't have to worry about non-primaries recursively included
        # from the job because groups can't include redirects, so any redirect
        # must either be a primary or have a parent who is a primary.
        #
        # All of this itertools stuff lets us iterate through the jobSet
        # with isPrimary set to True and then iterate through the jobs we
        # create here with isPrimary set to False.

        # The outer loop is to allow redirects to point to redirects. The
        # inner loop handles one set of troves.

        initialSet = itertools.izip(itertools.repeat(True), jobSet)
        while initialSet:
            alreadyHandled = set()
            nextSet = set()
            toDoSet = util.IterableQueue()
            for isPrimary, job in itertools.chain(initialSet, toDoSet):
                (name, (oldVersion, oldFlavor), (newVersion, newFlavor),
                    isAbsolute) = job
                item = (name, newVersion, newFlavor)

                if item in alreadyHandled:
                    continue
                alreadyHandled.add(item)

                if newVersion is None:
                    # Erasures don't involve redirects so they aren't 
                    # interesting.
                    continue

                trv = uJob.getTroveSource().getTrove(name, newVersion, 
                                                     newFlavor, 
                                                     withFiles = False)

                if not trv.isRedirect():
                    continue

                if item in redirectHack:
                    # this was from a redirect to a redirect -- the list
                    # of what needs to be removed as part of this redirect
                    # needs to move to the new target
                    redirectSourceList = redirectHack[item]
                    del redirectHack[item]
                else:
                    redirectSourceList = []

                if isPrimary:
                    # Don't install a redirect
                    jobsToRemove.append(job)
                else:
                    transitiveClosureToRemove.append(job)

                if not recurse:
                    raise UpdateError, \
                        "Redirect found with --no-recurse set: %s=%s[%s]" % item

                allTargets = [ (x[0], str(x[1].label()), x[2]) 
                                        for x in trv.iterRedirects() ]
                for troveSpec in allTargets:
                    if (troveSpec[0] == trv.getName()
                       and troveSpec[1] == str(trv.getVersion().trailingLabel())
                       and troveSpec[2] is None):
                        # this is an pre-1.2 redirect from one branch to
                        # another on the same label.  It only makes sense
                        # with the branch information attached.
                        allTargets = list(trv.iterRedirects())
                        break

                matches = self.repos.findTroves([], allTargets, self.cfg.flavor,
                                                affinityDatabase = self.db)
                if not matches:
                    # this is a remove redirect
                    assert(not allTargets)
                    l = redirectHack.setdefault(None, redirectSourceList)
                    l.append(item)
                else:
                    for matchList in matches.itervalues():
                        for match in matchList:
                            if match in redirectSourceList:
                                raise UpdateError, \
                                    "Redirect loop found which includes " \
                                    "troves %s, %s" % (item[0],
                                    ", ".join(x[0] for x in redirectSourceList))
                            assert(match not in redirectSourceList)
                            l = redirectHack.setdefault(match, 
                                                        redirectSourceList[:])
                            l.append(item)
                            redirectJob = (match[0], (None, None),
                                                     match[1:], True)
                            nextSet.add((isPrimary, redirectJob))
                            if isPrimary:
                                jobsToAdd.append(redirectJob)

                for info in trv.iterTroveList(strongRefs = True):
                    toDoSet.add((False, 
                                 (info[0], (None, None), info[1:], True)))

            # The targets of redirects need to be loaded - but only
            # if they're not already in the job.
            nextSet = list(nextSet)
            hasTroves = uJob.getTroveSource().hasTroves([
                            (x[1][0], x[1][2][0], x[1][2][1]) for x in nextSet])
            nextSet = [ x[0] for x in itertools.izip(nextSet, hasTroves) \
                                                                  if not x[1] ]

            redirectCs, notFound = csSource.createChangeSet(
                    [ x[1] for x in nextSet ],
                    withFiles = False, recurse = True)

            uJob.getTroveSource().addChangeSet(redirectCs)
            transitiveClosure.update(redirectCs.getJobSet(primaries = False))

            initialSet = nextSet

        # We may remove some jobs which we add due to redirection chains.
        jobSet.update(jobsToAdd)
        jobSet.difference_update(jobsToRemove)

        # remove redirects from transitive closure
        transitiveClosure.difference_update(transitiveClosureToRemove)
        transitiveClosure.difference_update(jobsToRemove)

        for l in redirectHack.itervalues():
            outdated = self.db.outdatedTroves(l)
            del l[:]
            for (name, newVersion, newFlavor), \
                  (oldName, oldVersion, oldFlavor) in outdated.iteritems():
                if oldVersion is not None:
                    l.append((oldName, oldVersion, oldFlavor))

        return redirectHack

    def _mergeGroupChanges(self, uJob, primaryJobList, transitiveClosure,
                           redirectHack, recurse, ineligible, checkPrimaryPins,
                           installedPrimaries, installMissingRefs=False, 
                           updateOnly=False, respectBranchAffinity=True,
                           alwaysFollowLocalChanges=False,
                           removeNotByDefault = False):

        def lookupPathHashes(infoList, old = False):
            result = []
            if old:
                for s in self.db.getPathHashesForTroveList(infoList):
                    if s is None:
                        result.append(set())
                    else:
                        result.append(s)

                return result
            else:
                troveSource = uJob.getTroveSource()
                # we can't assume that the troveSource has all
                # of the child troves for packages we can see.
                # Specifically, the troveSource could have come from
                # a relative changeset that only contains byDefault True
                # components - here we're looking at both byDefault 
                # True and False components (in trove.diff)
                hasTroveList = troveSource.hasTroves(infoList)
                for info, hasTrove in itertools.izip(infoList, hasTroveList):
                    if hasTrove:
                        ph = troveSource.getTrove(withFiles = False, *info).\
                                        getPathHashes()
                        result.append(ph)
                    else:
                        result.append(set())
            return result

	def _findErasures(primaryErases, newJob, referencedTroves, recurse,
                          ineligible):
	    # each node is a ((name, version, flavor), state, edgeList
	    #		       fromUpdate)
	    # state is ERASE, KEEP, or UNKNOWN
            #
            # fromUpdate is True if erasing this node reflects a trove being
            # replaced by a different one in the Job (an update, not an erase)
            # We need to track this to know what's being removed, but we don't
            # need to cause them to be removed.
	    nodeList = []
	    nodeIdx = {}
	    ERASE = 1
	    KEEP = 2
	    UNKNOWN = 3

            jobQueue = util.IterableQueue()
            # The order of this chain matters. It's important that we handle
            # all of the erasures we already know about before getting to the
            # ones which are implicit. That gets the state right for ones
            # which are explicit (since arriving there implicitly gets
            # ignored). Handling updates from newJob first prevents duplicates
            # from primaryErases
            for job, isPrimary in itertools.chain(
                        itertools.izip(newJob, itertools.repeat(False)),
                        itertools.izip(primaryErases, itertools.repeat(True)), 
                        jobQueue):

                oldInfo = (job[0], job[1][0], job[1][1])

                if oldInfo[1] is None: 
                    # skip new installs
                    continue  

                if oldInfo in nodeIdx:
                    # See the note above about chain order (this wouldn't
                    # work w/o it)
                    continue

                if not self.db.hasTrove(*oldInfo):
                    # no need to erase something we don't have installed
                    continue

                # XXX this needs to be batched
                pinned = self.db.trovesArePinned([ oldInfo ])[0]

                # erasures which are part of an
                # update are guaranteed to occur
                if job in newJob:
                    assert(job[2][0])
                    state = ERASE
                    fromUpdate = True
                else:
                    # If it's pinned, we keep it.
                    if pinned:
                        state = KEEP
                        if isPrimary and checkPrimaryPins:
                            raise UpdatePinnedTroveError(oldInfo)
                    elif isPrimary:
                        # primary updates are guaranteed to occur (if the
                        # trove is not pinned).
                        state = ERASE
                    else:
                        state = UNKNOWN

                    fromUpdate = False

                assert(oldInfo not in nodeIdx)
                nodeIdx[oldInfo] = len(nodeList)
                nodeList.append([ oldInfo, state, [], fromUpdate ])

                if not recurse: continue

                if not trove.troveIsCollection(oldInfo[0]): continue
                trv = self.db.getTrove(withFiles = False, pristine = False,
                                       withDeps = False, *oldInfo)

                for inclInfo in trv.iterTroveList(strongRefs=True):
                    # we only use strong references when erasing.
                    if inclInfo in ineligible:
                        continue

                    jobQueue.add(((inclInfo[0], inclInfo[1:], (None, None), 
                                  False), False))

            # For nodes which we haven't decided to erase, we need to track
            # down all of the collections which include those troves.
	    needParents = [ (nodeId, info) for nodeId, (info, state, edges,
                                                        alreadyHandled)
				in enumerate(nodeList) if state == UNKNOWN ]
	    while needParents:
		containers = self.db.getTroveContainers(
                                        x[1] for x in needParents)
                newNeedParents = []
		for (nodeId, nodeInfo), containerList in \
				itertools.izip(needParents, containers):
		    for containerInfo in containerList:
			if containerInfo in nodeIdx:
                            containerId = nodeIdx[containerInfo]
			    nodeList[containerId][2].append(nodeId)
			else:
			    containerId = len(nodeList)
			    nodeIdx[containerInfo] = containerId
			    nodeList.append([ containerInfo, KEEP, [ nodeId ],
                                              False])
			    newNeedParents.append((containerId, containerInfo))
                needParents = newNeedParents

            # don't erase nodes which are referenced by troves we're about
            # to install - the only troves we list here are primary installs,
            # and they should never be erased.
            for info in referencedTroves:
                if info in nodeIdx:
                    node = nodeList[nodeIdx[info]]
                    node[1] = KEEP

	    seen = [ False ] * len(nodeList)
            # DFS to mark troves as KEEP
            keepNodes = [ nodeId for nodeId, node in enumerate(nodeList)
                                if node[1] == KEEP ]
            while keepNodes:
                nodeId = keepNodes.pop()
                if seen[nodeId]: continue
                seen[nodeId] = True
                nodeList[nodeId][1] = KEEP
                keepNodes += nodeList[nodeId][2] 

            # anything which isn't to KEEP is to erase, but skip those which
            # are already being removed by a trvCs
            eraseList = ((x[0][0], (x[0][1], x[0][2]), (None, None), False)
                         for x in nodeList if x[1] != KEEP and not x[3])

            return set(eraseList)

        def _troveTransitiveClosure(db, itemList):
            itemQueue = util.IterableQueue()
            fullSet = set()
            for item in itertools.chain(itemList, itemQueue):
                if item in fullSet: continue
                fullSet.add(item)

                if not trove.troveIsCollection(item[0]): continue
                trv = db.getTrove(withFiles = False, pristine = False, *item)

                for x in trv.iterTroveList(strongRefs=True, weakRefs=True):
                    itemQueue.add(x)

            return fullSet

        def _filterDoubleRemovesDueToLocalUpdates(newJob, replacedJobs):
            # it's possible that a locally removed half of a local update
            # trove will match up to something that is due to be installed
            # despite the fact that its match has been removed by the user.
            # We don't want to disallow that type of match - it is useful
            # information and may be corrrect.  However, we don't want to
            # have a double removal, which is what will happen in some cases
            # - conary will switch the erased version mentioned in the local 
            # update to the installed version, which could be a part of 
            # another update.  In such cases, we remove the "erase" part
            # of the local update.
            eraseCount = {}
            for job in newJob:
                oldInfo = job[0], job[1][0], job[1][1]
                if oldInfo in replacedJobs:
                    if oldInfo in eraseCount:
                        eraseCount[oldInfo] += 1
                    else:
                        eraseCount[oldInfo] = 1

            doubleErased = [ x[0] for x in eraseCount.iteritems() if x[1] > 1 ]
            for oldInfo in doubleErased:
                newJob.remove((oldInfo[0], (oldInfo[1], oldInfo[2]),
                               replacedJobs[oldInfo], False))
                newJob.add((oldInfo[0], (None, None), 
                            replacedJobs[oldInfo], False))



        # def _mergeGroupChanges -- main body begins here
        erasePrimaries =    set(x for x in primaryJobList 
                                    if x[2][0] is None)
        relativePrimaries = set(x for x in primaryJobList 
                                    if x[2][0] is not None and
                                       not x[3])
        absolutePrimaries = set(x for x in primaryJobList 
                                    if x[3])
        assert(len(relativePrimaries) + len(absolutePrimaries) +
               len(erasePrimaries) == len(primaryJobList))

        log.lowlevel('_mergeGroupChanges(recurse=%s,'
                      ' checkPrimaryPins=%s,'
                      ' installMissingRefs=%s, '
                      ' updateOnly=%s, '
                      ' respectBranchAffinity=%s,'
                      ' alwaysFollowLocalChanges=%s)',
                      recurse, checkPrimaryPins, installMissingRefs,
                      updateOnly, respectBranchAffinity,
                      alwaysFollowLocalChanges)

        troveSource = uJob.getTroveSource()


        # ineligible needs to be a transitive closure when recurse is set
        if recurse:
            ineligible = _troveTransitiveClosure(self.db, ineligible)

        for job in erasePrimaries:
            # an erase primary can't be part of an update (but their children
            # can, so add this after we've recursed)
            ineligible.add((job[0], job[1][0], job[1][1]))

        # Build the trove which contains all of the absolute change sets
        # we may need to install. Build a set of all of the trove names
        # in that trove as well.
        availableTrove = trove.Trove("@update", versions.NewVersion(),
                                     deps.Flavor(), None)

        names = set()
        for job in transitiveClosure:
            if job[2][0] is None: continue
            if not job[3]: continue
            if (job[0], job[2][0], job[2][1]) in ineligible: continue

            availableTrove.addTrove(job[0], job[2][0], job[2][1],
                                    presentOkay = True)
            names.add(job[0])

        avail = set(availableTrove.iterTroveList(strongRefs=True))

        # Build the set of all relative install jobs (transitive closure)
        relativeUpdateJobs = set(job for job in transitiveClosure if
                                    job[2][0] is not None and not job[3])
        
        # Look for relative updates whose sources are not currently installed
        relativeUpdates = [ ((x[0], x[1][0], x[1][1]), x)
                    for x in relativeUpdateJobs if x[1][0] is not None]
        isPresentList = self.db.hasTroves([ x[0] for x in relativeUpdates ])

        for (info, job), isPresent in itertools.izip(relativeUpdates,
                                                     isPresentList):
            if not isPresent:
                relativeUpdateJobs.remove(job)
                newTrove = job[0], job[2][0], job[2][1]

                if newTrove not in avail:
                    ineligible.add(newTrove)

        # skip relative installs that are already present.
        relativeInstalls = [ ((x[0], x[2][0], x[2][1]), x)
                         for x in relativeUpdateJobs if x[2][0] is not None]
        isPresentList = self.db.hasTroves([ x[0] for x in relativeInstalls ])
        for (newTrove, job), isPresent in itertools.izip(relativeInstalls,
                                                         isPresentList):
            if isPresent:
                relativeUpdateJobs.remove(job)
                ineligible.add(newTrove)
                if job[1][0]:
                    # this used to be a relative upgrade, but the target
                    # is installed, so turn it into an erase.
                    erasePrimaries.add((job[0], (job[1][0], job[1][1]), 
                                                (None, None), False))

        # Get all of the currently installed and referenced troves which
        # match something being installed absolute. Troves being removed
        # through a relative changeset aren't allowed to be removed by
        # something else.
        (installedNotReferenced, 
         installedAndReferenced, 
         referencedStrong,
         referencedWeak) = self.db.db.getCompleteTroveSet(names)

        installedTroves = installedNotReferenced | installedAndReferenced
        referencedNotInstalled = referencedStrong | referencedWeak
        log.lowlevel('referencedNotInstalled: %s', referencedNotInstalled)
        log.lowlevel('ineligible: %s', ineligible)

        installedTroves.difference_update(ineligible)
        installedTroves.difference_update(
                (job[0], job[1][0], job[1][1]) for job in relativeUpdateJobs)
        referencedNotInstalled.difference_update(ineligible)
        referencedNotInstalled.difference_update(
                (job[0], job[1][0], job[1][1]) for job in relativeUpdateJobs)


        # The job between referencedTroves and installedTroves tells us
        # a lot about what the user has done to his system. 
        primaryLocalUpdates = self.getPrimaryLocalUpdates(names)
        localUpdates = list(primaryLocalUpdates)
        if localUpdates:
            localUpdates += self.getChildLocalUpdates(uJob.getSearchSource(),
                                                      localUpdates,
                                                      installedTroves,
                                                      referencedNotInstalled)
            # make some assertions about the local updates:
            # 1. a missing trove can only be a part of one local update
            # 2. a present trove can only be a part of one local update

            # although we needed parent updates to get the correct set of
            # local updates related to this job, we don't care local updates
            # that aren't related to troves in our job.
            localUpdates = [ x for x in localUpdates if x[0] in names ]

        localUpdatesByPresent = \
                 dict( ((job[0], job[2][0], job[2][1]), job[1]) for
                        job in localUpdates if job[1][0] is not None and
                                               job[2][0] is not None)
        localUpdatesByMissing = \
                 dict( ((job[0], job[1][0], job[1][1]), job[2]) for
                        job in localUpdates if job[1][0] is not None and
                                               job[2][0] is not None)
        primaryLocalUpdates = set((job[0], job[2][0], job[2][1]) 
                                  for job in primaryLocalUpdates)

        # Troves which were locally updated to version on the same branch
        # no longer need to be listed as referenced. The trove which replaced
        # it is always a better match for the new items (installed is better
        # than not installed as long as the branches are the same). This
        # doesn't apply if the trove which was originally installed is
        # part of this update though, as troves which are referenced and
        # part of the update are handled separately.

        # keep track of troves that are changes on the same branch, 
        # since those are still explicit user requests and might 
        # override implied updates that would downgrade this trove.
        sameBranchLocalUpdates = {}

        for job in sorted(localUpdates):
            if job[1][0] is not None and job[2][0] is not None:
                if (job[1][0].branch() == job[2][0].branch() and
                      (job[0], job[1][0], job[1][1]) not in avail):
                    del localUpdatesByPresent[(job[0], job[2][0], job[2][1])]
                    del localUpdatesByMissing[(job[0], job[1][0], job[1][1])]
                    referencedNotInstalled.remove((job[0], job[1][0], job[1][1]))
                    log.lowlevel('reworking same-branch local update: %s', job)
                    # track this update for since it means the user
                    # requested this version explicitly
                    sameBranchLocalUpdates[job[0], job[2][0], job[2][1]] = (job[1][0], job[1][1])
                else:
                    log.lowlevel('local update: %s', job)

        del localUpdates

        # Build the set of the incoming troves which are either already
        # installed or already referenced. 
        alreadyInstalled = (installedTroves & avail) | installedPrimaries
        alreadyReferenced = referencedNotInstalled & avail

        del avail


        existsTrv = trove.Trove("@update", versions.NewVersion(), 
                                deps.Flavor(), None)
        [ existsTrv.addTrove(*x) for x in installedTroves ]
        [ existsTrv.addTrove(*x) for x in referencedNotInstalled ]

        jobList = availableTrove.diff(existsTrv,
                                      getPathHashes=lookupPathHashes)[2]

        # alreadyReferenced troves are in both the update set 
        # and the installed set.  They are a good match for themselves.
        jobList += [(x[0], (x[1], x[2]), (x[1], x[2]), 0) for x in alreadyReferenced ]

        installJobs = [ x for x in jobList if x[1][0] is     None and
                                              x[2][0] is not None ]
        updateJobs = [ x for x in jobList if x[1][0] is not None and
                                             x[2][0] is not None ]
        pins = self.db.trovesArePinned([ (x[0], x[1][0], x[1][1]) 
                                                    for x in updateJobs ])
        jobByNew = dict( ((job[0], job[2][0], job[2][1]), (job[1], pin)) for
                        (job, pin) in itertools.izip(updateJobs, pins) )
        jobByNew.update(
                   dict( ((job[0], job[2][0], job[2][1]), (job[1], False)) for
                        job in installJobs))
        
        del jobList, installJobs, updateJobs

        # Relative jobs override pins and need to match up against the
        # right thing.
        jobByNew.update(
                    dict( ((job[0], job[2][0], job[2][1]), (job[1], False)) for
                        job in relativeUpdateJobs))

        respectFlavorAffinity = True
        # the newTroves parameters are described below.
        newTroves = sorted((((x[0], x[2][0], x[2][1]), 
                             True, {}, False, False, False, False, None, 
                             respectBranchAffinity, 
                             respectFlavorAffinity, True,
                             True, updateOnly)
                            for x in itertools.chain(absolutePrimaries,
                                                     relativePrimaries)),
                           # compare on the string of the version, since it might
                           # not have timestamps
                           key=lambda y: (y[0][0], str(y[0][1]), y[0][2]) + y[1:])

        newJob = set()
        notByDefaultRemovals = set()

        # ensure the user-specified respect branch affinity setting is not 
        # lost.
        neverRespectBranchAffinity = not respectBranchAffinity
        replacedJobs = {}

        while newTroves:
            # newTroves tuple values
            # newInfo: the (n, v, f) of the trove to install
            # isPrimary: true if user specified this trove on the command line
            # byDefaultDict: mapping of trove tuple to byDefault setting, as 
            #                specified by the primary parent trove 
            # parentInstalled: True if the parent of this trove was installed.
            #                  Used to determine whether to install troves 
            #                  with weak references.
            # parentReplacedWasPinned: True if this trove's parent would
            #                          have replaced a trove that is pinned.
            # parentUpdated: True if the parent of this trove was installed.
            #                or updated.
            # primaryInstalled: True if the primary that led to this update
            #                   was an install.
            # branchHint:  if newInfo's parent trove switched branches, this
            #              provides the to/from information on that switch.
            #              If this child trove is making the same switch, we 
            #              allow it even if the switch is overriding branch
            #              affinity.
            # respectBranchAffinity: If true, we generally try to respect
            #              the user's choice to switch a trove from one branch
            #              to another.  We might not respect branch affinity
            #              if a) a primary trove update is overriding branch
            #              affinity, or b) the call to mergeGroupChanges
            #              had respectBranchAffinity False
            # respectFlavorAffinity: If true, we generally try to respect
            #              the user's choice to switch a trove from one flavor
            #              to another.  We might not respect flavor affinity
            #              for the same reasons we might not respect branch
            #              affinity.
            # installRedirects: If True, we install redirects even when they
            #              are not upgrades.
            # followLocalChanges: see the code where it is used for a 
            #              description.
            # updateOnly:  If true, only update troves, don't install them
            #              fresh.
            (newInfo, isPrimary, byDefaultDict, parentInstalled,
             parentReplacedWasPinned, parentUpdated, primaryInstalled,
             branchHint, respectBranchAffinity, respectFlavorAffinity,
             installRedirects, followLocalChanges,
             updateOnly) = newTroves.pop(0)

            byDefault = isPrimary or byDefaultDict[newInfo]

            log.lowlevel('''\
*******
%s=%s[%s]
primary: %s  byDefault:%s  parentUpdated: %s parentInstalled: %s primaryInstalled: %s updateOnly: %s
branchHint: %s
branchAffinity: %s   flavorAffinity: %s installRedirects: %s
followLocalChanges: %s

''',
                      newInfo[0], newInfo[1], newInfo[2], isPrimary,
                      byDefault, parentUpdated, parentInstalled,
                      primaryInstalled, updateOnly, branchHint,
                      respectBranchAffinity, respectFlavorAffinity,
                      installRedirects, followLocalChanges)
            trv = None
            jobAdded = False
            replaced = (None, None)
            recurseThis = True
            childrenFollowLocalChanges = alwaysFollowLocalChanges
            pinned = False
            while True:
                # this loop should only be called once - it's basically a 
                # way to create a quick GOTO structure, without needing 
                # to call another function (which would be expensive in 
                # this loop).
                if newInfo in alreadyInstalled:
                    # No need to install it twice
                    # but count it as 'added' for the purposes of
                    # whether or not to recurse
                    jobAdded = True
                    job = (newInfo[0], (newInfo[1], newInfo[2]),
                                       (newInfo[1], newInfo[2]), False)
                    log.lowlevel('SKIP: already installed')
                    break
                elif newInfo in ineligible:
                    log.lowlevel('SKIP: ineligible')
                    break
                elif newInfo in alreadyReferenced:
                    log.lowlevel('new trove in alreadyReferenced')
                    # meaning: this trove is referenced by something 
                    # installed, but is not installed itself.

                    if isPrimary or installMissingRefs or primaryInstalled:
                        # They really want it installed this time. We removed
                        # this entry from the already-installed @update trove
                        # so localUpdates already tells us the best match for it.
                        pass
                    elif parentUpdated and newInfo in referencedWeak:
                        # The only link to this trove is a weak reference.
                        # A weak-only reference means an intermediate trove 
                        # was missing.  But parentUpdated says we've now
                        # updated an intermediate trove, so install
                        # this trove too.
                        pass
                    else:
                        # We already know about this trove, and decided we
                        # don't want it. We do want to keep the item which
                        # replaced it though.
                        if newInfo in localUpdatesByMissing:
                            info = ((newInfo[0],) 
                                     + localUpdatesByMissing[newInfo])
                            alreadyInstalled.add(info)
                            log.lowlevel('local update - marking present part %s'
                                      'as already installed', info)
                        log.lowlevel('SKIP: already referenced')
                        break

                replaced, pinned = jobByNew[newInfo]
                replacedInfo = (newInfo[0], replaced[0], replaced[1])

                log.lowlevel('replaces: %s', replacedInfo)

                if replaced[0] is not None:
                    if newInfo in alreadyReferenced:
                        # This section is the corrolary to the section
                        # above.  We only enter here if we've decided
                        # to install this trove even though its 
                        # already referenced.  
                        if replacedInfo in referencedNotInstalled:
                            # don't allow this trove to not be installed
                            # because the trove its replacing is not installed.
                            # Find an installed update or just install the trove
                            # fresh.
                            replaced = localUpdatesByMissing.get(replacedInfo, 
                                                                 (None, None))
                            replacedInfo = (replacedInfo[0], replaced[0], 
                                            replaced[1])
                            log.lowlevel('replaced is not installed, using local update %s instead', replacedInfo)
                            if replaced[0]:
                                log.lowlevel('following local changes')
                                childrenFollowLocalChanges = True
                                replacedJobs[replacedInfo] = (newInfo[1], newInfo[2])
                                pinned = self.db.trovesArePinned([replacedInfo])[0]
                    elif replacedInfo in referencedNotInstalled:
                        # the trove on the local system is one that's referenced
                        # but not installed, so, normally we would not install
                        # this trove.
                        # BUT if this is a primary (or in certain other cases)
                        # we always want to have the update happen.  
                        # In the case of a primary trove, 
                        # if the referenced trove is replaced by another trove 
                        # on the the system (by a localUpdate) then we remove 
                        # that trove instead.  If not, we just install this 
                        # trove as a fresh update. 
                        log.lowlevel('replaced trove is not installed')
                        if (not ((parentInstalled
                                 and not parentReplacedWasPinned)
                                 or followLocalChanges
                                 or installMissingRefs)):
                            # followLocalChanges states that, even though
                            # the given trove is not a primary, we still want
                            # replace a localUpdate if available instead of 
                            # skipping the update.  This flag can be set if
                            # a) an ancestor of this trove is a primary trove
                            # that switched from a referencedNotInstalled
                            # to an installed trove or b) its passed in to
                            # the function that we _always_ follow local 
                            # changes.
                            log.lowlevel('SKIP: not following local changes')
                            break

                        freshInstallOkay = (isPrimary or
                                            (parentInstalled
                                             and not parentReplacedWasPinned)
                                            or installMissingRefs)
                        # we always want to install the trove even if there's
                        # no local update to match to if it's a primary, or
                        # if the trove's parent was just installed 
                        # (if the parent was installed, we just added a
                        # strong reference, which overrides any other
                        # references that might suggest not to install it.)

                        replaced = localUpdatesByMissing.get(replacedInfo, 
                                                             (None, None))


                        if (replaced[0] is None and not freshInstallOkay):
                            log.lowlevel('SKIP: not allowing fresh install')
                            break

                        childrenFollowLocalChanges = True

                        replacedInfo = (replacedInfo[0], replaced[0], 
                                        replaced[1])
                        replacedJobs[replacedInfo] = (newInfo[1], newInfo[2])
                        if replaced[0]:
                            pinned = self.db.trovesArePinned([replacedInfo])[0]
                        log.lowlevel('using local update to replace %s, following local changes', replacedInfo)

                    elif not installRedirects:
                        if not redirectHack.get(newInfo, True):
                            # a parent redirect was added as an upgrade
                            # but this would be a new install of this child
                            # trove.  Skip it.
                            log.lowlevel('SKIP: is a redirect that would be'
                                      ' a fresh install, but '
                                      ' installRedirects=False')
                            break
                    elif redirectHack.get(newInfo, False):
                        # we are upgrading a redirect, so don't allow any child
                        # redirects to be installed unless they have a matching
                        # trove to redirect on the system.
                        log.lowlevel('INSTALL: upgrading redirect')
                        installRedirects = False

                    if replaced[0] and respectBranchAffinity: 
                        log.lowlevel('checking branch affinity')
                        # do branch affinity checks

                        newBranch = newInfo[1].branch()
                        installedBranch = replacedInfo[1].branch()

                        if replacedInfo in localUpdatesByPresent:
                            notInstalledVer = localUpdatesByPresent[replacedInfo][0]
                            notInstalledBranch = notInstalledVer.branch()
                            # create alreadyBranchSwitch variable for 
                            # readability
                            alreadyBranchSwitch = True
                        else:
                            notInstalledBranch = None
                            alreadyBranchSwitch = False


                        # Check to see if there's reason to be concerned
                        # about branch affinity.
                        if installedBranch == newBranch:
                            log.lowlevel('not branch switch')
                            # we didn't switch branches.  No branch 
                            # affinity concerns.  If the user has made
                            # a local change that would make this new 
                            # install a downgrade, skip it.
                            if (not isPrimary
                                and newInfo[1] < replaced[0]
                                and replacedInfo in sameBranchLocalUpdates
                                and (replacedInfo in primaryLocalUpdates 
                                     or not parentUpdated)):
                                    log.lowlevel('SKIP: avoiding downgrade')

                                    # don't let this trove be erased, pretend
                                    # like it was explicitly requested.
                                    alreadyInstalled.add(replacedInfo)
                                    break
                        elif notInstalledBranch == installedBranch:
                            log.lowlevel('INSTALL: branch switch is reversion')
                            # we are reverting back to the branch we were
                            # on before.  We don't worry about downgrades
                            # because we're already overriding the user's
                            # branch choice
                            pass
                        else:
                            log.lowlevel('is a new branch switch')
                            # Either a) we've made a local change from branch 1
                            # to branch 2 and now we're updating to branch 3,
                            # or b) there's no local change but we're switching
                            # branches.

                            # Generally, we respect branch affinity and don't
                            # do branch switches.  There 
                            # are a few exceptions:
                            if isPrimary:
                                # the user explicitly asked to switch
                                # to this branch, so we have to honor it.

                                if alreadyBranchSwitch:
                                    # it turns out the _current_ installed
                                    # trove is a local change.  The user
                                    # is messing with branches too much -
                                    # don't bother with branch affinity.
                                    respectBranchAffinity = False
                                    log.lowlevel('INSTALL: is a branch switch on top of a branch switch and is primary')
                                else:
                                    log.lowlevel('INSTALL: is a new branch switch and is primary')
                            elif (installedBranch, newBranch) == branchHint:
                                # Exception: if the parent trove
                                # just made this move, then allow it.
                                log.lowlevel('INSTALL: matches parent\'s branch switch')
                                pass
                            elif ((replacedInfo in installedAndReferenced
                                   or replacedInfo in sameBranchLocalUpdates)
                                  and not alreadyBranchSwitch
                                  and parentUpdated):
                                # Exception: The user has not switched this
                                # trove's branch explicitly, and now
                                # we have an implicit request to switch 
                                # the branch.
                                log.lowlevel('INSTALL: implicit branch switch, parent installed')
                                pass
                            else:
                                # we're not installing this trove - 
                                # It doesn't match any of our exceptions.
                                # It could be that it's a trove with
                                # no references to it on the system
                                # (and so a branch switch would be strange)
                                # or it could be that it is a switch
                                # to a third branch by the user.
                                # Since we're rejecting the update due to 
                                # branch affinity, we don't consider any of its 
                                # child troves for updates either.
                                log.lowlevel('SKIP: not installing branch switch')
                                recurseThis = False 
                                alreadyInstalled.add(replacedInfo)
                                break

                        if replaced[0] and respectFlavorAffinity:
                            if replacedInfo in localUpdatesByPresent:
                                notInstalledFlavor = localUpdatesByPresent[replacedInfo][1]
                                # create alreadyBranchSwitch variable for 
                                # readability
                                alreadyFlavorSwitch = True
                            elif replacedInfo in sameBranchLocalUpdates:
                                notInstalledFlavor = sameBranchLocalUpdates[replacedInfo][1]
                            else:
                                notInstalledFlavor = None

                            if (notInstalledFlavor is not None
                                and not deps.compatibleFlavors(notInstalledFlavor, replacedInfo[2])
                                and not deps.compatibleFlavors(replacedInfo[2], newInfo[2])):
                                if isPrimary:
                                    respectFlavorAffinity = False
                                else:
                                    log.lowlevel('SKIP: Not reverting'
                                              ' incompatible flavor switch')
                                    recurseThis = False
                                    alreadyInstalled.add(replacedInfo)
                                    break



                # below are checks to see if a fresh install should completed.
                # Since its possible that an update from above could be 
                # converted into a fresh install, we start a new if/elif 
                # branch here.
                if replaced[0]:
                    # we are dealing with a replacement, we've already 
                    # decided it was okay above.
                    pass
                elif not byDefault:
                    # This trove is being newly installed, but it's not 
                    # supposed to be installed by default
                    log.lowlevel('SKIP: not doing not-by-default fresh install')
                    break
                elif updateOnly:
                    # we're not installing trove, only updating installed
                    # troves.
                    log.lowlevel('SKIP: not doing install due to updateOnly')
                    break
                elif not isPrimary and self.cfg.excludeTroves.match(newInfo[0]):
                    # New trove matches excludeTroves
                    log.lowlevel('SKIP: trove matches excludeTroves')
                    break
                elif not installRedirects:
                    if not redirectHack.get(newInfo, True):
                        # a parent redirect was added as an upgrade
                        # but this would be a new install of this child
                        # trove.  Skip it.
                        log.lowlevel('SKIP: redirect would be a fresh install')
                        break
                elif redirectHack.get(newInfo, False):
                    # we are upgrading a redirect, so don't allow any child
                    # redirects to be installed unless they have a matching
                    # trove to redirect on the system.
                    log.lowlevel('installing redirect')
                    installRedirects = False

                job = (newInfo[0], replaced, (newInfo[1], newInfo[2]), False)
                if pinned and (not isPrimary or checkPrimaryPins):
                    job = self._splitPinnedJob(uJob, troveSource, job,
                                               force=not isPrimary)
                    if job is None:
                        recurseThis = False
                        break
                    elif (not isPrimary 
                          and self.cfg.excludeTroves.match(newInfo[0])):
                        # New trove matches excludeTroves
                        log.lowlevel('SKIP: trove matches excludeTroves')
                        recurseThis = False
                        break

                log.lowlevel('JOB ADDED: %s', job)
                newJob.add(job)
                jobAdded = True
                break

            log.lowlevel('recurseThis: %s\nrecurse: %s', recurseThis, recurse)

            if jobAdded and removeNotByDefault and not byDefault:
                job = (newInfo[0], replaced, (newInfo[1], newInfo[2]), False)
                newJob.discard(job)
                if replaced[0]:
                    notByDefaultRemovals.add(
                                (newInfo[0], replaced, (None, None), False))
                elif newInfo in alreadyInstalled:
                    notByDefaultRemovals.add(
                                (newInfo[0], (newInfo[1], newInfo[2]),
                                             (None, None), False))

            if not recurseThis: continue
            if not recurse: continue
            if not trove.troveIsCollection(newInfo[0]): continue


            branchHint = None
            if replaced[0] and replaced[0].branch() == newInfo[1].branch():
                # if this trove didn't switch branches, then we respect branch
                # affinity for all child troves even the primary trove above us
                # did switch.  We assume the user at some point switched this 
                # trove to the desired branch by hand already.
                log.lowlevel('respecting branch affinity for children')
                if not neverRespectBranchAffinity:
                    respectBranchAffinity = True
            elif replaced[0]:
                branchHint = (replaced[0].branch(), newInfo[1].branch())

            if replaced[0] and deps.compatibleFlavors(replaced[1], newInfo[2]):
                log.lowlevel('respecting flavor affinity for children')
                respectFlavorAffinity = True

            if trv is None:
                try:
                    trv = troveSource.getTrove(withFiles = False, *newInfo)
                except TroveMissing:
                    if self.db.hasTrove(*newInfo):
                        trv = self.db.getTrove(withFiles = False, *newInfo)
                    else:
                        # it's possible that the trove source we're using
                        # contains references to troves that it does not 
                        # actually contain.  That's okay as long as the
                        # excluded trove is not actually trying to be
                        # installed.
                        if jobAdded:
                            raise
                        else:
                            continue

            if isPrimary:
                # byDefault status of troves is determined by the primary
                # trove.  
                byDefaultDict = dict((x[0], x[1]) \
                                            for x in trv.iterTroveListInfo())

            updateOnly = updateOnly or not jobAdded
            # for all children, we only want to install them as new _if_ we 
            # installed their parent.  If we did not install/upgrade foo, then
            # we do not install foo:runtime (though if it's installed, it
            # is reasonable to upgrade it).

            if isPrimary:
                primaryInstalled = jobAdded and not job[1][0]
            
            jobInstall = primaryInstalled or (jobAdded and not job[1][0])

            for info in sorted(trv.iterTroveList(strongRefs=True)):

                if not isPrimary:
                    if not jobAdded and info not in byDefaultDict:
                        continue

                    # support old-style collections.  _If_ this trove was not
                    # mentioned in its parent trove, then set its default
                    # value here. 
                    childByDefault = (trv.includeTroveByDefault(*info)
                                      and jobAdded)
                    byDefaultDict.setdefault(info, childByDefault)

                newTroves.append((info, False,
                                  byDefaultDict, jobInstall, pinned, jobAdded,
                                  primaryInstalled,
                                  branchHint, respectBranchAffinity,
                                  respectFlavorAffinity, installRedirects,
                                  childrenFollowLocalChanges,
                                  updateOnly))

        _filterDoubleRemovesDueToLocalUpdates(newJob, replacedJobs)
        for job in notByDefaultRemovals:
            if job not in newJob:
                erasePrimaries.add((job[0], job[1], (None, None), False))
            alreadyInstalled.discard((job[0], job[1][0], job[1][1]))

        # items which were updated to redirects should be removed, no matter
        # what
        for info in set(itertools.chain(*redirectHack.values())):
            erasePrimaries.add((info[0], (info[1], info[2]), (None, None), False))

	eraseSet = _findErasures(erasePrimaries, newJob, alreadyInstalled, 
                                 recurse, ineligible)
        assert(not x for x in newJob if x[2][0] is None)
        newJob.update(eraseSet)
        return newJob

    def _splitPinnedJob(self, uJob, troveSource, job, force=False):
        def _getPathHashes(trvSrc, db, trv, inDb = False):
            if not trv.isCollection(): return trv.getPathHashes()

            ph = None
            for info in trv.iterTroveList(strongRefs=True):
                # FIXME: should this include weak references?
                if inDb:
                    otherTrv = db.getTrove(withFiles = False, *info)
                elif trvSrc.hasTrove(*info):
                    otherTrv = trvSrc.getTrove(withFiles = False, *info)
                else:
                    # if the trove is not in the trove source, then it 
                    # can't be part of the update job.  This can happen 
                    # for example if you're just installing a package with
                    # no-recurse.
                    continue

                if ph is None:
                    ph = otherTrv.getPathHashes()
                else:
                    ph.update(otherTrv.getPathHashes())

            if ph is None:
                # this gives us an empty set
                ph = trv.getPathHashes()

            return ph


        if job[1][0] is None:
            return job

        newInfo = (job[0], job[2][0], job[2][1])
        replacedInfo = (job[0], job[1][0], job[1][1])
        log.lowlevel('looking at pinned replaced trove')
        try:
            trv = troveSource.getTrove(withFiles = False, *newInfo)
        except TroveMissing:
            # we don't even actually have this trove available,
            # making it difficult to install.
            log.lowlevel('SKIP: new trove is not in source,'
                      ' cannot compare path hashes!')
            return None

        # try and install the two troves next to each other
        oldTrv = self.db.getTrove(withFiles = False, 
                                  pristine = False, 
                                  *replacedInfo)
        oldHashes = _getPathHashes(troveSource, self.db, 
                                   oldTrv, inDb = True)
        newHashes = _getPathHashes(uJob.getTroveSource(), 
                                   self.db, trv, inDb = False)

        if not newHashes.compatibleWith(oldHashes):
            if force:
                return None
            else:
                raise UpdatePinnedTroveError(replacedInfo, 
                                             newInfo)

        log.lowlevel('old and new versions are compatible')
        replaced = (None, None)
        if not force:
            name = replacedInfo[0]
            self.updateCallback.warning(
"""
Not removing old %s as part of update - it is pinned.
Installing new version of %s side-by-side instead.

To remove the old %s, run:
conary unpin '%s=%s[%s]'
conary erase '%s=%s[%s]'
""", *((name, name, name) + replacedInfo + replacedInfo))
        return (job[0], (None, None), job[2], False)

    def _findOverlappingJobs(self, jobSet, troveSource):
        """
            Returns a list of sets of jobs, where each set has the 
            following property:
                for every job in the set, there is another job in the set
                    such that:
                    1) the job removes a path that the other job adds
                    2) the job adds a path that other job removes
                    3) both jobs add the same path
                    or
                    4) both jobs remove the same path (though this should
                       be impossible because conary only allows one trove
                       to own a file)
             All sets in a job should be connected to each other through
             some chain of these relationships.
        """
        # overlapping is a dict from jobSet id -> overlapping id OR
        # jobSet id -> list of other ids that overlap.
        # for example, overlapping[3] -> 2, and overlapping[2] -> [2,3]
        # would be reasonable, meaning that the set at id 2 contains
        # all the overlapping troves there.  
        overlapping = {}

        # d is a dict of pathHash -> id of first job that has that 
        # pathHash.
        d = {}

        jobSet = list(enumerate(jobSet))

        pathHashesList = self.db.getPathHashesForTroveList(
                                           (x[0], x[1][0], x[1][1])
                                           for idx, x in jobSet if x[1][0])
        oldJobs = itertools.izip((x for x in jobSet if x[1][1][0]),
                                 pathHashesList)

        getHashes = troveSource.getPathHashesForTroveList
        newJobs = ((x, getHashes([(x[1][0], x[1][2][0], x[1][2][1])])[0])
                   for x in jobSet if x[1][2][0])

        for ((idx, job), pathHashes) in itertools.chain(oldJobs, newJobs):
            if pathHashes is None:
                continue
            for pathHash in pathHashes:
                if pathHash not in d:
                    d[pathHash] = idx
                else:
                    if d[pathHash] == idx:
                        continue
                    # someone else already had this issue.  Find out
                    # what overlapping set they are in and add ourselves to 
                    # it.
                    newIdx = d[pathHash]

                    if idx in overlapping:
                        # if we're already part of a set, find our set.
                        while isinstance(overlapping[idx], int):
                            idx = overlapping[idx]

                        if newIdx in overlapping and overlapping[newIdx] == idx:
                            # in this case newIdx is already a part of our 
                            # set.
                            continue
                        overlapping[idx].append(newIdx)
                    else:
                        # create a new set consisting of ourselves and 
                        # newIdx.
                        overlapping[idx] = [idx, newIdx]

                    if newIdx in overlapping:
                        # if newIdx was already part of a set,
                        # find that set and extend our set with it.
                        while isinstance(overlapping[newIdx], int):
                            oldIdx = overlapping[newIdx]
                            overlapping[newIdx] = idx
                            newIdx = oldIdx
                        if newIdx == idx:
                            continue

                        overlapping[idx].extend(overlapping[newIdx])

                    # we've joined newIdx (and maybe all its friends) into
                    # our set, so now point newIdx to our set.
                    overlapping[newIdx] = idx

        sets = []
        for val in overlapping.itervalues():
            if isinstance(val, int):
                continue
            sets.append([ jobSet[x][1] for x in set(val) ])
        return sets

    def _trovesNotFound(self, notFound):
        """
            Raises a nice error message when changeset creation failed
            to include all the necessary troves.
        """
        nonLocal = [ x for x in notFound if not x[2][0].isOnLocalHost() ]
        if nonLocal:
            troveList = '\n   '.join(['%s=%s[%s]' % (x[0], x[2][0], x[2][1]) 
                                     for x in nonLocal])
            raise UpdateError(
                   'Failed to find required troves for update:\n   %s' 
                    % troveList)


    def _updateChangeSet(self, itemList, uJob, keepExisting = None, 
                         recurse = True, updateMode = True, sync = False,
                         useAffinity = True, checkPrimaryPins = True,
                         forceJobClosure = False, ineligible = set(),
                         syncChildren=False, updateOnly=False,
                         installMissing = False, removeNotByDefault = False,
                         exactFlavors = False):
        """
        Updates a trove on the local system to the latest version 
        in the respository that the trove was initially installed from.

        @param itemList: List specifying the changes to apply. Each item
        in the list must be a ChangeSetFromFile, or a standard job tuple.
        Versions in the job tuple may be strings, versions, branches, or 
        None. Flavors may be None.
        @type itemList: list
        """
        searchSource = uJob.getSearchSource()
        if not isinstance(searchSource, searchsource.AbstractSearchSource):
            if searchSource.isSearchAsDatabase():
                searchSource = searchsource.SearchSource(
                                                  uJob.getSearchSource(),
                                                  self.cfg.flavor, self.db)
            else:
                searchSource = searchsource.NetworkSearchSource(
                                                  uJob.getSearchSource(),
                                                  self.cfg.installLabelPath,
                                                  self.cfg.flavor, self.db)
            uJob.setSearchSource(searchSource)

        def _separateInstalledItems(jobSet):
            present = self.db.hasTroves([ (x[0], x[2][0], x[2][1]) for x in 
                                                    jobSet ] )
            oldItems = set([ (job[0], job[2][0], job[2][1]) for job, isPresent 
                                in itertools.izip(jobSet, present) 
                                if isPresent ])
            newItems = set([ job for job, isPresent 
                                in itertools.izip(jobSet, present) 
                                if not isPresent ])
            return newItems, oldItems

        def _jobTransitiveClosure(db, troveSource, jobSet):
            # This is an expensive operation. Use it carefully.
            jobQueue = util.IterableQueue()
            jobClosure = set()

            for job in itertools.chain(jobSet, jobQueue):
                if job in jobClosure:
                    continue

                if job[2][0] is None:
                    continue

                jobClosure.add(job)
                if not trove.troveIsCollection(job[0]): continue

                if job[1][0] is None:
                    oldTrv = None
                else:
                    oldTrv = db.getTroves([(job[0], job[1][0], job[1][1])],
                                     withFiles = False, pristine = False)[0]
                    if oldTrv is None:
                        # XXX batching these would be much more efficient
                        oldTrv = troveSource.getTrove(job[0], job[1][0],
                                                      job[1][1],
                                                      withFiles = False)

                try:
                    newTrv = troveSource.getTrove(job[0], job[2][0], job[2][1],
                                                  withFiles = False)
                except (TroveMissing, OpenError), err:
                    # In the case where we're getting transitive closure
                    # for a relative changeset and hit a trove that is
                    # not included in the relative changeset (because it's
                    # already installed locally), grab it from the local 
                    # database instead.
                    newTrv = db.getTroves([(job[0], job[2][0], job[2][1])],
                                           withFiles = False,
                                           pristine = True)[0]
                    # If it not there, maybe we're not installing it anyway.
                    # We'll let the troveMissing error occur when we actually
                    # try to install this trove.
                    if newTrv is None:
                        continue

                recursiveJob = newTrv.diff(oldTrv, absolute = job[3])[2]
                for x in recursiveJob:
                    jobQueue.add(x)

            return jobClosure

        # def _updateChangeSet -- body starts here
        if syncChildren:
            installMissing = True

        # This job describes updates from a networked repository. Duplicates
        # (including installing things already installed) are skipped.
        newJob = set()
        # These are items being removed.
        removeJob = set()
        # This is the full, transitive closure of the job
        transitiveClosure = set()

        toFind = {}
        toFindNoDb = {}
        for item in itemList:
            (troveName, (oldVersionStr, oldFlavorStr),
                        (newVersionStr, newFlavorStr), isAbsolute) = item
            assert(oldVersionStr is None or not isAbsolute)

            if troveName[0] == '-':
                needsOld = True
                needsNew = newVersionStr or (newFlavorStr is not None)
                troveName = troveName[1:]
            elif troveName[0] == '+':
                needsNew = True
                needsOld = oldVersionStr or (oldFlavorStr is not None)
                troveName = troveName[1:]
            else:
                needsOld = oldVersionStr or (oldFlavorStr is not None)
                needsNew = newVersionStr or (newFlavorStr is not None)
                if not (needsOld or needsNew):
                    if updateMode:
                        needsNew = True
                    else:
                        needsOld = True

            if needsOld:
                oldTroves = self.db.findTrove(None, 
                                   (troveName, oldVersionStr, oldFlavorStr))
            else:
                oldTroves = []

            if not needsNew:
                assert(newFlavorStr is None)
                assert(not isAbsolute)
                for troveInfo in oldTroves:
                    log.lowlevel("set up removal of %s", troveInfo)
                    removeJob.add((troveInfo[0], (troveInfo[1], troveInfo[2]),
                                   (None, None), False))
                # skip ahead to the next itemList
                continue

            if len(oldTroves) > 2:
                raise UpdateError, "Update of %s specifies multiple " \
                            "troves for removal" % troveName
            elif oldTroves:
                oldTrove = (oldTroves[0][1], oldTroves[0][2])
            else:
                oldTrove = (None, None)
            del oldTroves

            if isinstance(newVersionStr, versions.Version):
                assert(isinstance(newFlavorStr, deps.Flavor))
                jobToAdd = (troveName, oldTrove,
                            (newVersionStr, newFlavorStr), isAbsolute)
                newJob.add(jobToAdd)
                log.lowlevel("set up job %s", jobToAdd)
                del jobToAdd
            elif isinstance(newVersionStr, versions.Branch):
                toFind[(troveName, newVersionStr.asString(),
                        newFlavorStr)] = oldTrove, isAbsolute
            elif (newVersionStr and newVersionStr[0] == '/'):
                # fully qualified versions don't need branch affinity
                # but they do use flavor affinity
                toFind[(troveName, newVersionStr, newFlavorStr)] = \
                                        oldTrove, isAbsolute
            else:
                if not (isAbsolute or not useAffinity):
                    # not isAbsolute means keepExisting. when using
                    # keepExisting, branch affinity doesn't make sense - we are
                    # installing a new, generally unrelated version of this
                    # trove
                    toFindNoDb[(troveName, newVersionStr, newFlavorStr)] \
                                    = oldTrove, isAbsolute
                else:
                    toFind[(troveName, newVersionStr, newFlavorStr)] \
                                    = oldTrove, isAbsolute
        results = {}
        searchSource = uJob.getSearchSource()

        if not useAffinity:
            results.update(searchSource.findTroves(toFind, useAffinity=False,
                                                   exactFlavors=exactFlavors))
        else:
            if toFind:
                results.update(searchSource.findTroves(toFind,
                                                    useAffinity=True,
                                                    exactFlavors=exactFlavors))
                log.lowlevel("looking up troves w/ database affinity")
            if toFindNoDb:
                log.lowlevel("looking up troves w/o database affinity")
                results.update(searchSource.findTroves(toFindNoDb,
                                                   useAffinity=False,
                                                   exactFlavors=exactFlavors))
        for troveSpec, (oldTroveInfo, isAbsolute) in \
                itertools.chain(toFind.iteritems(), toFindNoDb.iteritems()):
            resultList = results[troveSpec]

            if len(resultList) > 1 and oldTroveInfo[0] is not None:
                raise UpdateError, "Relative update of %s specifies multiple " \
                            "troves for install" % troveName

            newJobList = [ (x[0], oldTroveInfo, x[1:], isAbsolute) for x in 
                                    resultList ]
            newJob.update(newJobList)
            log.lowlevel("adding jobs %s", newJobList)

        # Items which are already installed shouldn't be installed again. We
        # want to track them though to ensure they aren't removed by some
        # other action.

        if not installMissing:
            jobSet, oldItems = _separateInstalledItems(newJob)
        else:
            # keep our original jobSet, we'll recurse through installed
            # items as well.
            jobSet, oldItems = newJob, _separateInstalledItems(newJob)[1]

        log.lowlevel("items already installed: %s", oldItems)

        jobSet.update(removeJob)
        del newJob, removeJob

        # we now have two things
        #   1. oldItems -- items which we should not remove as a side effect
        #   2. jobSet -- job we need to create a change set for

        if not jobSet:
            raise NoNewTrovesError

        # FIXME changeSetSource: I should just be able to call 
        # csSource.createChangeSet but it can't handle recursive
        # createChangeSet calls, and you can only call createChangeSet
        # once on a csSource.  So, we avoid having to call it more than
        # once by checking to see if the changeSets are already in the
        # update job.
        hasTroves = uJob.getTroveSource().hasTroves(
            [ (x[0], x[2][0], x[2][1]) for x in jobSet ] )

        reposChangeSetList = set([ x[1] for x in
                          itertools.izip(hasTroves, jobSet)
                           if x[0] is not True ])

        if reposChangeSetList != jobSet:
            # we can't trust the closure from the changeset we're getting
            # since we're not getting everything for jobSet
            forceJobClosure = True

        csSource = trovesource.stack(uJob.getSearchSource(),
                                     self.repos)

        cs, notFound = csSource.createChangeSet(reposChangeSetList,
                                                withFiles = False,
                                                recurse = recurse)
        self._replaceIncomplete(cs, csSource, 
                                self.db, self.repos)
        if notFound:
            self._trovesNotFound(notFound) # may raise an error 
                                           # if there are non-local troves
                                           # in the list
            jobSet.difference_update(notFound)
            if not jobSet:
                raise NoNewTrovesError
        assert(not notFound)
        uJob.getTroveSource().addChangeSet(cs)
        transitiveClosure = set(cs.getJobSet(primaries = False))
        del cs

        redirectHack = self._processRedirects(csSource, uJob, jobSet, 
                                              transitiveClosure, recurse) 

        if forceJobClosure and recurse:
            # The transitiveClosure we computed can't be trusted; we need
            # to build another one. We could do this all the time, but it's
            # expensive
            transitiveClosure = _jobTransitiveClosure(self.db,
                                            trovesource.stack(
                                                uJob.getTroveSource(),
                                                self.repos), jobSet)
            # Since we couldn't trust the transitive closure generated,
            # we need to check to see if any of the recursive troves we'll
            # need are not in the changeset.  This will be true 
            # of group changesets.
            transitiveJobs = list(transitiveClosure)
            hasTroves = uJob.getTroveSource().hasTroves(
                            (x[0], x[2][0], x[2][1]) for x in transitiveJobs)

            reposChangeSetList = set([ x[1] for x in
                              itertools.izip(hasTroves, transitiveJobs)
                               if x[0] is not True ])

            csSource = trovesource.stack(uJob.getSearchSource(),
                                         self.repos)
            trovesExist = csSource.hasTroves(
                            [(x[0], x[2][0], x[2][1]) for x in reposChangeSetList])
            reposChangeSetList = [ x[1] for x in
                                   itertools.izip(trovesExist, reposChangeSetList)
                                   if x[0] is True ]
            cs, notFound = csSource.createChangeSet(reposChangeSetList,
                                                    withFiles = False,
                                                    recurse = recurse)
            self._replaceIncomplete(cs, csSource, self.db, self.repos)
            #NOTE: we allow any missing bits (recursive or not) to be skipped.
            # We may not install then anyways
            #They'll show up in notFound.
            #assert(not notFound)
            uJob.getTroveSource().addChangeSet(cs)
        elif forceJobClosure:
            transitiveClosure = jobSet
        # else we trust the transitiveClosure which was passed in

        if not installMissing:
            # we know that all the troves in jobSet are already installed
            # (i.e. in oldItems) when syncing.  We don't want to exclude 
            # their children from syncing
            ineligible = ineligible | oldItems

        newJob = self._mergeGroupChanges(uJob, jobSet, transitiveClosure,
                                 redirectHack, recurse, ineligible, 
                                 checkPrimaryPins, 
                                 installedPrimaries=oldItems, 
                                 installMissingRefs=installMissing,
                                 updateOnly=updateOnly,
                                 respectBranchAffinity=not installMissing,
                                 alwaysFollowLocalChanges=installMissing,
                                 removeNotByDefault = removeNotByDefault)

        if not newJob:
            raise NoNewTrovesError

        removedTroves = list()
        missingTroves = list()
        rollbackFence = False
        ts = uJob.getTroveSource()
        for job in newJob:
            if job[2][0] is None:
                continue

            cs = ts.getChangeSet(job)
            troveCs = cs.getNewTroveVersion(job[0], job[2][0], job[2][1])
            if troveCs.troveType() == trove.TROVE_TYPE_REMOVED:
                ti = trove.TroveInfo(troveCs.troveInfoDiff.freeze())
                if ti.flags.isMissing():
                    missingTroves.append(job)
                else:
                    removedTroves.append(job)

            if job[1][0] is not None:
                oldCompatClass = self.db.getTroveCompatibilityClass(
                        job[0], job[1][0], job[1][1])
                # it's an update; check for preupdate scripts
                preScript = troveCs.getPreUpdateScript()
                if preScript:
                    uJob.addJobPreScript(job, preScript, oldCompatClass,
                                         troveCs.getNewCompatibilityClass())
            else:
                oldCompatClass = None

            rollbackFence = rollbackFence or \
                troveCs.isRollbackFence(update = (job[1][0] is not None),
                                        oldCompatibilityClass = oldCompatClass)

        uJob.setInvalidateRollbacksFlag(rollbackFence)

        if removedTroves or missingTroves:
            removed = [ (x[0], x[2][0], x[2][1]) for x in removedTroves ]
            removed.sort()
            missing = [ (x[0], x[2][0], x[2][1]) for x in missingTroves ]
            missing.sort()
            raise MissingTrovesError(missing, removed)

        uJob.setPrimaryJobs(jobSet)

        return newJob

    def _fullMigrate(self, itemList, uJob, recurse=True):
        def _convertRedirects(searchSource, newTroves):
            troveNames = set(x.getName() for x in newTroves)
            redirects = [ x for x in newTroves if x.isRedirect() ]
            nonRedirects = [ x for x in newTroves if not x.isRedirect() ]
            if not redirects:
                return newTroves, troveNames

            redirectMap = {}
            toFind = {}
            for trv in redirects:
                redirTup = trv.getNameVersionFlavor()
                redirectMap[redirTup] = [redirTup]
                for troveName, branch, flavor in trv.iterRedirects():
                    troveSpec = (troveName, str(branch), flavor)
                    toFind.setdefault(troveSpec, []).append(redirTup)

            if not toFind:
                if not nonRedirects:
                    err = ("Cannot migrate to redirect(s), as they are all"
                           " erases - \n%s" % \
                           "\n".join("%s=%s[%s]" % x.getNameVersionFlavor()
                                     for x in redirects))
                    raise UpdateError(err)
                else:
                    return nonRedirects, troveNames

            while toFind:
                matches = searchSource.findTroves(toFind, useAffinity=True)
                allTroveTups = list(set(itertools.chain(*matches.itervalues())))
                allTroves = searchSource.getTroves(allTroveTups)
                allTroves = dict(itertools.izip(allTroveTups, allTroves))

                newToFind = {}
                for troveSpec, troveTupList in matches.iteritems():
                    for troveTup in troveTupList:
                        redirTups = toFind[troveSpec]
                        for redirTup in redirTups:
                            if redirTup == troveTup:
                                err = "Redirect Loop detected - trove %s=%s[%s] redirects to itself" % redirTup
                                raise UpdateError(err)
                            elif redirTup in redirectMap.get(troveTup, []):
                                err = "Redirect Loop detected - "
                                err += "includes %s=%s[%s] and %s=%s[%s]" % (redirTup + troveTup)
                                raise UpdateError(err)
                            else:
                                redirectMap.setdefault(troveTup, []).append(redirTup)
                        trv = allTroves[troveTup]
                        if trv.isRedirect():
                            for troveName, branch, flavor in trv.iterRedirects():
                                newTroveSpec = (troveName, str(branch), flavor)
                                newToFind.setdefault(newTroveSpec, []).append(troveTup)
                        else:
                            nonRedirects.append(trv)
                            troveNames.add(trv.getName())
                toFind = newToFind
            return set(nonRedirects), troveNames
  
        def _getTrovesToBeMigrated(db, troves, troveNames):
            """
                Gets the list of troves on the system 
                that will be migrated to the new troves in the update
                job (just the top level troves)
            """
            # perform a diff of toplevel troves to find out 
            # what version of the troves on the system will be updated.
            existsTrv = trove.Trove("@update", versions.NewVersion(),
                                    deps.Flavor(), None)
            availableTrv = trove.Trove("@update", versions.NewVersion(),
                                         deps.Flavor(), None)
            existsTups = []
            availTups = []
            for troveNVF in db.findByNames(troveNames):
                existsTups.append(troveNVF)
                existsTrv.addTrove(*troveNVF)

            for trv in troves:
                availTups.append(trv.getNameVersionFlavor())
                availableTrv.addTrove(*trv.getNameVersionFlavor())

            oldTroves = set(existsTups) & set(availTups) # oldVer == newVer
                                                         # won't show up in 
                                                         # the diff, so grab
                                                         # separately
            jobs = availableTrv.diff(existsTrv)[2]

            oldTroves.update((x[0], x[1][0], x[1][1]) for x in jobs if x[1][0])

            return db.getTroves(oldTroves, withFiles=False)

        toFind = []
        for item in itemList:
            (troveName, (oldVersionStr, oldFlavorStr),
                        (newVersionStr, newFlavorStr), isAbsolute) = item
            if (not isAbsolute and oldVersionStr) or troveName[0] == '-':
                raise UpdateError('Cannot perform relative updates '
                                  'or erases as part of full migration')
            toFind.append((troveName, newVersionStr, newFlavorStr))

        searchSource = uJob.getSearchSource()

        results = searchSource.findTroves(toFind, useAffinity=True)
        newTroves = list(set(itertools.chain(*results.itervalues())))
        newTroves = searchSource.getTroves(newTroves, withFiles=False)

        newTroves, troveNames = _convertRedirects(searchSource, newTroves)

        updateSet = []

        availByDefaultInfo = {}

        for trv in newTroves:
            updateSet.append(trv.getNameVersionFlavor())
            availByDefaultInfo[trv.getNameVersionFlavor()] = True
            if not recurse:
                continue
            for (troveNVF, byDefault, isWeak) in trv.iterTroveListInfo():
                updateSet.append(troveNVF)
                if byDefault:
                    availByDefaultInfo[troveNVF] = True
                else:
                    availByDefaultInfo.setdefault(troveNVF, False)

        # find the installed versions of the top level troves we're
        # trying to migrate to.  Troves within the _old_ versions 
        # of those troves that installed by byDefault False 
        # we will keep when migrating.
        # E.g. if you install foo:debuginfo, when we migrate you,
        # foo:debuginfo will be kept.
        toBeMigrated = _getTrovesToBeMigrated(self.db, newTroves, troveNames)

        byDefaultFalse = []
        byDefaultTrue = []
        count = 0
        notByDefault = []
        for trv in toBeMigrated:
            if not recurse:
                continue
            troveList = list(trv.iterTroveListInfo())

            troveTups = [x[0] for x in troveList]
            hasTroves = self.db.hasTroves(troveTups)
            troveList = (x[0] for x in itertools.izip(troveList, hasTroves) if x[1])
            for (troveNVF, byDefault, isWeak) in troveList:
                count += 1
                if not byDefault:
                    byDefaultFalse.append(troveNVF)
                else:
                    byDefaultTrue.append(troveNVF)
        byDefaultFalse = set(byDefaultFalse)
        byDefaultFalse.difference_update(byDefaultTrue)
        del byDefaultTrue

        updateSet = set(updateSet)
        eraseSet = set(self.db.iterAllTroves())
        toKeep = eraseSet & updateSet
        updateSet.difference_update(toKeep)
        eraseSet.difference_update(toKeep)

        existsTrv = trove.Trove("@update", versions.NewVersion(),
                                deps.Flavor(), None)
        availableTrv = trove.Trove("@update", versions.NewVersion(),
                                     deps.Flavor(), None)
        for troveNVF in updateSet:
            availableTrv.addTrove(*troveNVF)
        for troveNVF in eraseSet:
            existsTrv.addTrove(*troveNVF)
        potentialJobs = availableTrv.diff(existsTrv)[2]

        finalJobs = []
        for job in potentialJobs:
            newInfo = (job[0], job[2][0], job[2][1])
            oldInfo = (job[0], job[1][0], job[1][1])
            if not job[1][0]:
                if not availByDefaultInfo[newInfo]:
                    # only install byDefault True
                    continue
            elif job[2][0]:
                if oldInfo not in byDefaultFalse:
                    # this is an update from a byDefault True trv 
                    # to a byDefault False trv.
                    if not availByDefaultInfo[newInfo]:
                        finalJobs.append((job[0], job[1], (None, None), False))
                        continue
            finalJobs.append(job)


        for troveNVF in toKeep:
            if (not availByDefaultInfo[troveNVF] 
                and troveNVF not in byDefaultFalse):
                finalJobs.append((troveNVF[0], (troveNVF[1], troveNVF[2]),
                                 (None, None), False))
        finalJobs = set(finalJobs)

        if not finalJobs:
            raise NoNewTrovesError

        updateJobs = set(x for x in finalJobs if x[2][0])

        removalJobs = [ x for x in finalJobs if x[1][0] ]

        pins = self.db.trovesArePinned([(x[0], x[1][0], x[1][1])
                                        for x in removalJobs])

        csSource = trovesource.stack(uJob.getSearchSource(), self.repos)

        for job, isPinned in itertools.izip(removalJobs, pins):
            if isPinned:
                if not job[2][0]:
                    # this is an erasure of a pinned trove skip it.
                    finalJobs.remove(job)
                    continue

                newJob = self._splitPinnedJob(uJob, csSource, job, force=True)
                if newJob is not None:
                    finalJobs.remove(job)
                    updateJobs.remove(job)
                    finalJobs.add(newJob)
                    updateJobs.add(newJob)
                else:
                    # we can't update this because of the pin.
                    # just leave the old version in place.
                    finalJobs.remove(job)
                    updateJobs.remove(job)

        updateJobs = list(updateJobs)
        hasTroves = uJob.getTroveSource().hasTroves(
            [ (x[0], x[2][0], x[2][1]) for x in updateJobs ] )

        reposChangeSetList = set([ x[1] for x in
                          itertools.izip(hasTroves, updateJobs)
                           if x[0] is not True ])

        cs, notFound = csSource.createChangeSet(reposChangeSetList,
                                                withFiles = False,
                                                recurse = False,
                                                callback = self.updateCallback)
        if notFound:
            self._trovesNotFound(notFound) # may raise an error
                                           # if there are non-local troves
                                           # in the list
            reposChangeSetList.difference_update(notFound)
            if not reposChangeSetList:
                raise NoNewTrovesError

        troveSource = uJob.getTroveSource()
        self._replaceIncomplete(cs, csSource, self.db, self.repos)
        troveSource.addChangeSet(cs)

        rollbackFence = None
        # XXX this is horrible; we probablt have everything we need already,
        # I just don't know how to find it
        infoCs = troveSource.createChangeSet(finalJobs, withFiles = False)
        assert(not infoCs[1])
        infoCs = infoCs[0]
        for job in finalJobs:
            if job[2][0] is None:
                continue

            troveCs = infoCs.getNewTroveVersion(job[0], job[2][0], job[2][1])

            if job[1][0] is not None:
                # it's an update; check for preupdate scripts
                oldCompatClass = self.db.getTroveCompatibilityClass(
                        job[0], job[1][0], job[1][1])
                preScript = troveCs.getPreUpdateScript()
                if preScript:
                    uJob.addJobPreScript(job, preScript, oldCompatClass,
                                         troveCs.getNewCompatibilityClass())
            else:
                oldCompatClass = None

            rollbackFence = rollbackFence or \
                troveCs.isRollbackFence(update = (job[1][0] is not None),
                                        oldCompatibilityClass = oldCompatClass)

        uJob.setInvalidateRollbacksFlag(rollbackFence)

        return finalJobs

    def getUpdateItemList(self):
        # Returns top-level items, things that need to be updated
        # in order to update entire system.
        items = ( x for x in self.getPrimaryLocalUpdates() 
                  if (x[1][0] is None
                      or not deps.compatibleFlavors(x[1][1], x[2][1])
                      or x[1][0].branch() != x[2][0].branch()))
        items = [ (x[0], x[2][0], x[2][1]) for x in items
                   if not x[2][0].isOnLocalHost() and 
                   not x[2][0].isInLocalNamespace() ]
        items = [ x[0] for x in itertools.izip(items,
                                               self.db.trovesArePinned(items))
                                                                  if not x[1] ]
        return items

    def fullUpdateItemList(self):
        # ignore updates that just switch version, not flavor or 
        # branch
        items = self.getUpdateItemList()

        installed = self.db.findByNames(x[0] for x in items)

        installedDict = {}
        for (name, version, release) in installed:
            branchDict = installedDict.setdefault(name, {})
            l = branchDict.setdefault(version.branch(), [])
            l.append((version, release))

        updateItems = []

        for name, version, flavor in items:
            branch = version.branch()
            verInfo = installedDict[name][branch]

            if len(installedDict[name]) == 1 and len(verInfo) == 1:
                updateItems.append((name, None, None))
                continue
            elif len(verInfo) == 1:
                updateItems.append((name, branch, None))
                continue

            score = None
            for instFlavor in self.cfg.flavor:
                newScore = instFlavor.score(flavor) 
                if newScore is False:
                    continue
                if score is None or newScore > score:
                    score = newScore
                    finalFlavor = instFlavor

            if score is not None:
                # otherwise, we'll search all flavors on update using affinity.
                flavor = deps.overrideFlavor(finalFlavor, flavor)

            if len(installedDict[name]) == 1:
                updateItems.append((name, None, flavor))
            else:
                updateItems.append((name, branch, flavor))

        return updateItems

    def getPrimaryLocalUpdates(self, troveNames=None):
        """
            Returns a set of changes (jobs) that explain how the user is likely
            to have modified their system to get it to its current state.

            The changes made are the top-level jobs, that is, if the user
            updated foo (which includes foo:runtime) from branch a to branch b,
            an update job for foo will be returned but not foo:runtime.

            If troveNames are specified, then the changes returned are those
            _related_ to the given trove name.  They may include changes 
            of troves with other names, however.  For example, if you 
            request changes for troves named foo, and foo is included by
            group-dist, and the only change related to foo you have made
            is installing group-dist, then a job showing the install of
            group-dist will be returned.

            @rtype: list of jobs
        """
        if troveNames is not None and not troveNames:
            return []


        allJobs = []        # allJobs is returned from this fn

        noParents = []      # troves with no parents that could be part of
                            # unknown local updates.

        troves = []         # troveId -> troveInfo map (troveId == index)
                            # contains (troveTup, isPresent, hasParent, 
                            #           isWeak)

        maxId = 0           # next index for troves list
        troveIdsByInfo = {} # (name,ver,flavor) -> troveId 

        parentIds = {}      # name -> [parents of troves w/ name, troveIds]
        childIds = {}       # troveId -> childIds

        TROVEINFO = 0
        ISPRESENT = 1
        HASPARENT = 2
        ISWEAK = 3

        # 1. Create needed data structures
        #    troves, parentIds, childIds
        for (troveInfo, parentInfo, isPresent, weakRef) \
                                in self.db.iterUpdateContainerInfo(troveNames):
            troveId = troveIdsByInfo.setdefault(troveInfo, maxId)
            if troveId == maxId:
                maxId += 1
                troves.append([troveInfo, isPresent, bool(parentInfo), weakRef])
            else:
                if isPresent:
                    troves[troveId][ISPRESENT] = True
                if parentInfo:
                    troves[troveId][HASPARENT] = True
                if not weakRef:
                    troves[troveId][ISWEAK] = False

            if parentInfo:
                parentId = troveIdsByInfo.setdefault(parentInfo, maxId)
                if parentId == maxId:
                    maxId += 1
                    troves.append([parentInfo, False, False, True])

            l = parentIds.setdefault(troveInfo[0], [set(), []])
            l[1].append(troveId)

            if parentInfo:
                childIds.setdefault(parentId, []).append(troveId)
                l[0].add(parentId)

        del maxId

        # remove troves that don't are not present and have no parents - they 
        # won't be part of local updates.
        allTroves = set(x[0] for x in enumerate(troves) 
                        if (x[1][ISPRESENT] or x[1][HASPARENT])
                           and not (x[1][ISPRESENT] and x[1][HASPARENT] and not x[1][ISWEAK]))
        for name, (parents, troveIds) in parentIds.items():
            parents.intersection_update(allTroves)
            parentIds[name][1] = set(troveIds)
            parentIds[name][1].intersection_update(allTroves)
        del allTroves

        noParents = (x[1][1] for x in parentIds.iteritems() if not x[1][0])
        noParents = set(itertools.chain(*noParents))

        while noParents:
            exists = trove.Trove('@update', versions.NewVersion(),
                                 deps.Flavor(), None)
            refd = trove.Trove('@update', versions.NewVersion(),
                               deps.Flavor(), None)

            for troveId in noParents:
                info, isPresent, hasParent, isWeak = troves[troveId]
                if isPresent:
                    exists.addTrove(presentOkay=True, *info)
                else:
                    refd.addTrove(presentOkay=True, *info)

            updateJobs = [  ]

            for job in exists.diff(refd)[2]:
                if not job[2][0]:
                    continue
                newInfo = troves[troveIdsByInfo[job[0], job[2][0], job[2][1]]]
                if not job[1][0] and newInfo[HASPARENT]:
                    # it's a new install.  If it has a parent,
                    # then it's already covered by the install of that
                    # parent.
                    continue
                elif newInfo[HASPARENT] and newInfo[ISWEAK]:
                    oldInfo = troves[troveIdsByInfo[job[0], job[1][0], job[1][1]]]
                    if oldInfo[ISWEAK]:
                        continue
                allJobs.append(job)

            # we've created all local updates related to this set of
            # troves - remove them as parents of other troves to generate
            # next noParent set.
            toDiscard = {}
            for troveId in noParents:
                for childId in childIds.get(troveId, []):
                    toDiscard.setdefault(troves[childId][0][0],
                                         []).append(troveId)

            newNoParents = []
            for name, troveIds in toDiscard.iteritems():
                parentIds[name][0].difference_update(troveIds)
                if not parentIds[name][0]:
                    newNoParents.extend(parentIds[name][1])
            del toDiscard

            noParents = set(newNoParents) - noParents

        return allJobs

    def getChildLocalUpdates(self, searchSource, localUpdates,
                             installedTroves=None, missingTroves=None):
        """
            Given a set of primary local updates - the updates the user
            is likely to have typed at the command line, return their
            child updates.  Given a primary update from a -> b, we look 
            at the children of a and b and see if a child of a is not 
            installed where a child of b is, and assert that that update is 
            from childa -> childb.
        """
        localUpdates = [ x for x in localUpdates 
                         if x[1][0] and not x[1][0].isOnLocalHost() ]
        oldTroveTups = [ (x[0], x[1][0], x[1][1]) for x in localUpdates ]
        newTroveTups = [ (x[0], x[2][0], x[2][1]) for x in localUpdates ]

        oldTroveSource = trovesource.stack(searchSource, self.repos)
        oldTroves = oldTroveSource.getTroves(oldTroveTups, withFiles=False)
        newTroves = self.db.getTroves(newTroveTups, withFiles=False)

        if installedTroves is None:
            assert(missingTroves is None)
            chain = itertools.chain
            izip = itertools.izip
            childNew = list(set(chain(*(x.iterTroveList(strongRefs=True,
                                                           weakRefs=True)
                                                        for x in newTroves))))
            childOld = list(set(chain(*(x.iterTroveList(strongRefs=True,
                                                        weakRefs=True)
                                                        for x in oldTroves))))
            hasTroves = self.db.hasTroves(childNew + childOld)
            installedTroves = set(x[0] for x in izip(childNew, hasTroves) 
                                                if x[1])
            installedTroves.update(newTroveTups)

            hasTroves = hasTroves[len(childNew):]
            missingTroves = set(x[0] for x in izip(childOld, hasTroves) 
                                              if not x[1])
            missingTroves.update(oldTroveTups)
            del childNew, childOld, hasTroves
        else:
            assert(missingTroves is not None)
            installedTroves = installedTroves.copy()
            missingTroves = missingTroves.copy()

        allJobs = []
        for oldTrove, newTrove in itertools.izip(oldTroves, newTroves):
            # find the relevant local updates by performing a 
            # diff between oldTrove and a trove based on newTrove
            # that contains only those parts of newTrove that are actually
            # installed.

            notExistsOldTrove = trove.Trove('@update', versions.NewVersion(), deps.Flavor())
            existsNewTrove = trove.Trove('@update', versions.NewVersion(), deps.Flavor())

            # only create local updates between old troves that
            # don't exist and new troves that do.
            for tup, _, isStrong in oldTrove.iterTroveListInfo():
                if (tup in missingTroves and tup not in oldTroveTups
                    and not newTrove.hasTrove(*tup)):
                    notExistsOldTrove.addTrove(*tup)
            for tup, _, isStrong in newTrove.iterTroveListInfo():
                if (tup in installedTroves and tup not in newTroveTups
                    and not oldTrove.hasTrove(*tup)):
                    existsNewTrove.addTrove( *tup)

            newUpdateJobs = existsNewTrove.diff(notExistsOldTrove)[2]

            for newJob in newUpdateJobs:
                if not newJob[1][0] or not newJob[2][0]:
                    continue

                # no trove should be part of more than one update.
                installedTroves.remove((newJob[0], newJob[2][0], newJob[2][1]))
                missingTroves.remove((newJob[0], newJob[1][0], newJob[1][1]))
                allJobs.append(newJob)
        return allJobs

    def _replaceIncomplete(self, cs, localSource, db, repos):
        jobSet = [ (x.getName(), (x.getOldVersion(), x.getOldFlavor()),
                                 (x.getNewVersion(), x.getNewFlavor()))
                    for x in cs.iterNewTroveList() ]
    
        incompleteJobs = [ x for x in jobSet 
                           if x[1][0] and x[2][0]
                              and not x[2][0].isOnLocalHost()
                              and db.hasTrove(x[0], *x[1]) 
                              and db.troveIsIncomplete(x[0], *x[1]) ]
        if incompleteJobs:
            newTroves = repos.getTroves([(x[0], x[2][0], x[2][1])
                                         for x in incompleteJobs])
            oldTroves = localSource.getTroves([(x[0], x[1][0], x[1][1])
                                               for x in incompleteJobs])
            newCs = changeset.ChangeSet()
            for newT, oldT in itertools.izip(newTroves, oldTroves):
                oldT.troveInfo.incomplete.set(1)
                newT.troveInfo.incomplete.set(1)
                newT.troveInfo.completeFixup.set(1)
                newCs.newTrove(newT.diff(oldT)[0])

            cs.merge(newCs)

    def loadRestartInfo(self, restartInfo):
        return _loadRestartInfo(restartInfo, self.lzCache)

    def saveRestartInfo(self, updJob, remainingJobs):
        return _storeJobInfo(remainingJobs, updJob)

    def cleanRestartInfo(self, restartInfo):
        if not restartInfo:
            return
        util.rmtree(restartInfo, ignore_errors=True)

    def newUpdateJob(self):
        """Create a new update job
        The job can be initialized either by using prepareUpdateJob or by
        thawing it from a frozen representation.
        @return: the new update job
        """
        updJob = database.UpdateJob(self.db, lazyCache = self.lzCache)
        return updJob

    def prepareUpdateJob(self, updJob, itemList, keepExisting = False,
                        recurse = True,
                        resolveDeps = True, test = False,
                        updateByDefault = True,
                        split = True, sync = False, fromChangesets = [],
                        checkPathConflicts = True, checkPrimaryPins = True,
                        resolveRepos = True, syncChildren = False,
                        updateOnly = False, resolveGroupList=None,
                        installMissing = False, removeNotByDefault = False,
                        keepRequired = None, migrate = False,
                        criticalUpdateInfo=None, resolveSource = None,
                        applyCriticalOnly = False, restartInfo = None,
                        exactFlavors = False):
        """
        Populates an update job based on a set of trove update and erase
        operations.If self.cfg.autoResolve is set, dependencies
        within the job are automatically closed. Returns a mapping with
        suggestions for possible dependency resolutions.

        @param updJob: An UpdateJob object
        @type updJob: conary.local.database.UpdateJob object
	@param itemList: A list of change specs:
        (troveName, (oldVersionSpec, oldFlavor), (newVersionSpec, newFlavor),
        isAbsolute).  isAbsolute specifies whether to try to find an older
        version of trove on the system to replace if none is specified.
	If updateByDefault is True, trove names in itemList prefixed
	by a '-' will be erased. If updateByDefault is False, troves without a
	prefix will be erased, but troves prefixed by a '+' will be updated.
        itemList can be None if restartInfo is set (see below).
        @type itemList: [(troveName, (oldVer, oldFla), 
                         (newVer, newFla), isAbs), ...]
	@param keepExisting: If True, troves updated not erase older versions
	of the same trove, as long as there are no conflicting files in either
	trove.
        @type keepExisting: bool
	@param keepRequired: If True, troves are not erased when they
        are the target of a dependency for a trove which is retained.
        @type keepRequired: bool
        @param recurse: Apply updates/erases to troves referenced by containers.
        @type recurse: bool
        @param resolveDeps: Should dependencies error be flagged or silently
        ignored?
        @type resolveDeps: bool
        @param test: If True, the operations will be attempted but the 
	filesystem and database will not be updated.
        @type test: bool
	@param updateByDefault: If True, troves passed to L{itemList} without a
	'-' or '+' prefix will be updated. If False, troves without a prefix 
	will be erased.
        @type updateByDefault: bool
        @param split: Split large update operations into separate jobs. As
                      of 1.0.10, this must be true (False broke how we
                      handle users and groups, which requires info- packages
                      to be installed first and in separate jobs)
        @type split: bool
        @param sync: Limit acceptabe trove updates only to versions 
        referenced in the local database.
        @type sync: bool
        @param fromChangesets: When specified, these changesets are used
        as the source of troves instead of the repository.
        @type fromChangesets: list of changeset.ChangeSetFromFile
        @param checkPathConflicts: check that applying the update job would
        not create path conflicts (True by default).
        @type checkPathConflicts: bool
        @param checkPrimaryPins: If True, pins on primary troves raise a 
        warning if an update can be made while leaving the old trove in place,
        or an error, if the update/erase cannot be made without removing the 
        old trove.
        @type checkPrimaryPins: bool
        @param resolveRepos: If True, search the repository for resolution
        troves.
        @type resolveRepos: bool
        @param syncChildren: If True, sync child troves so that they match
        the references in the specified troves.
        @type syncChildren: bool
        @param updateOnly: If True, do not install missing troves, just
        update installed troves.
        @type updateOnly: bool
        @param installMissing: If True, always install missing troves
        @type installMissing: bool
        @param removeNotByDefault: remove child troves that are not by default.
        @type removeNotByDefault: bool
        @param criticalUpdateInfo: Settings and data needed for critical
        updates
        @type: CriticalUpdateInfo instance
        @param resolveSource: Instance of 
        conaryclient.resolve.DepResolutionMethod to be used for dep resolution.
        If left blank will be created based on installLabelPath or 
        resolveGroups.
        @type: conaryclient.resolveDepResolutionMethod instance
        @param applyCriticalOnly: apply only the critical update.
        @type applyCriticalOnly: bool
        @param restartInfo: If specified, overrides itemList. It specifies the
        location where the rest of an update job run was stored (after
        applying the critical update).
        @type restartInfo: string
        @rtype: dict
        """
        if keepRequired is None:
            keepRequired = self.cfg.keepRequired

        if self.updateCallback is None:
            self.setUpdateCallback(UpdateCallback())

        if not criticalUpdateInfo:
            criticalUpdateInfo = CriticalUpdateInfo(applyCriticalOnly)

        restartChangeSets = []
        if restartInfo:
            # ignore itemList passed in, we load it from the restart info
            itemList, restartChangeSets = self.loadRestartInfo(restartInfo)
            recurse = False
            syncChildren = False    # we don't recalculate update info anyway
                                    # so we'll just revert to regular update.
            migrate = False

        if syncChildren:
            for name, oldInf, newInfo, isAbs in itemList:
                if not isAbs:
                    raise ConaryError(
                            'cannot specify erases/relative updates with sync')

        # Add information from the stored update job, if available
        for cs, includesFileContents in restartChangeSets:
            criticalUpdateInfo.addChangeSet(cs, includesFileContents)

        try:
            (updJob, suggMap) = self.updateChangeSet(itemList,
                    keepExisting = keepExisting,
                    recurse = recurse,
                    resolveDeps = resolveDeps,
                    test = test,
                    updateByDefault = updateByDefault,
                    split = split,
                    sync = sync,
                    fromChangesets = fromChangesets,
                    checkPathConflicts = checkPathConflicts,
                    checkPrimaryPins = checkPrimaryPins,
                    resolveRepos = resolveRepos,
                    syncChildren = syncChildren,
                    updateOnly = updateOnly,
                    resolveGroupList = resolveGroupList,
                    installMissing = installMissing,
                    removeNotByDefault = removeNotByDefault,
                    keepRequired = keepRequired,
                    migrate = migrate,
                    criticalUpdateInfo = criticalUpdateInfo,
                    resolveSource = resolveSource,
                    updateJob = updJob, exactFlavors = exactFlavors)
        except DependencyFailure, e:
            raise
        except:
            if restartChangeSets:
                log.error('** NOTE: A critical update was applied - rerunning this command may resolve this error')
            raise

        return suggMap

    def applyUpdateJob(self, updJob, replaceFiles = None, tagScript = None,
                    test = False, justDatabase = False, journal = None,
                    localRollbacks = None, autoPinList = None,
                    keepJournal = False, noRestart=False,
                    replaceManagedFiles = False,
                    replaceUnmanagedFiles = False,
                    replaceModifiedFiles = False,
                    replaceModifiedConfigFiles = False):
        """
        Apply the update job.
        @param updJob: An UpdateJob object.
        @type updJob: conary.local.database.UpdateJob object
        @param replaceFiles: Replace locally changed files (deprecated).
        @type replaceFiles: bool
        @param tagScript:
        @type tagScript:
        @param test: Dry-run, don't perform any changes.
        @type test: bool
        @param justDatabase: If set, no filesystem changes will be performed
        (changes are limited to the database).
        @type justDatabase: bool
        @param journal:
        @type journal:
        @param localRollbacks: Store the complete rollback information in the
        rollback directory (without referring to the changesets in the
        repository). This allows the system to apply rollbacks without
        connecting the repository, at the expense of disk space consumption.
        The setting defaults to the value of self.cfg.localRollbacks.
        @type localRollbacks: bool
        @autoPinList: A list of troves that will not change. Defaults to the
        value from self.cfg.pinList
        @type autoPinList: list
        @keepJournal: If set, the conary journal file will be left behind
        (useful only for debugging journal cleanup routines)
        @type keepJournal: bool
        @noRestart: If set, suppresses the restart after critical updates
        behavior default to conary.
        @type noRestart: bool
        @return: None if the update was fully applied, or restart information
        if a critical update was applied and a restart is necessary to make it
        active.
        """
        # A callback object must be supplied
        assert(self.updateCallback is not None)

        if localRollbacks is None:
            localRollbacks = self.cfg.localRollbacks

        # In migrate mode we replace modified and unmanaged files (CNY-1868)
        # This can be overridden with arguments
        if updJob.getKeywordArguments().get('migrate', False):
            replaceModifiedFiles = True
            replaceUnmanagedFiles = True

        if replaceFiles is not None:
            replaceManagedFiles = replaceFiles
            replaceUnmanagedFiles = replaceFiles
            replaceModifiedFiles = replaceFiles
            replaceModifiedConfigFiles = replaceFiles

        commitFlags = database.CommitChangeSetFlags(
            replaceManagedFiles = replaceManagedFiles,
            replaceUnmanagedFiles = replaceUnmanagedFiles,
            replaceModifiedFiles = replaceModifiedFiles,
            replaceModifiedConfigFiles = replaceModifiedConfigFiles,
            justDatabase = justDatabase,
            localRollbacks = localRollbacks,
            test = test, keepJournal = keepJournal)

        if autoPinList is None:
            autoPinList = self.cfg.pinTroves

        # Apply the update job, return restart information if available
        if noRestart:
            # Apply everything
            remainingJobs = []
        else:
            # Load just the critical jobs (or everything if no critical jobs
            # are present)
            remainingJobs = updJob.loadCriticalJobsOnly()

        # XXX May have to use a callback for this
        log.syslog.command()
        self._applyUpdate(updJob, tagScript = tagScript, journal = journal,
                          autoPinList = autoPinList, commitFlags = commitFlags)
        log.syslog.commandComplete()

        if remainingJobs:
            # FIXME: write the updJob.getTroveSource() changeset(s) to disk
            # write the job set to disk
            # Restart conary telling it to use those changesets and jobset
            # (ignore ordering).
            # do depresolution on that job set to compare contents and warn
            # if contents have changed.
            restartDir = self.saveRestartInfo(updJob, remainingJobs)
            return restartDir

        return None


    def updateChangeSet(self, itemList, keepExisting = False, recurse = True,
                        resolveDeps = True, test = False,
                        updateByDefault = True, callback=None,
                        split = True, sync = False, fromChangesets = [],
                        checkPathConflicts = True, checkPrimaryPins = True,
                        resolveRepos = True, syncChildren = False,
                        updateOnly = False, resolveGroupList=None,
                        installMissing = False, removeNotByDefault = False,
                        keepRequired = None, migrate = False,
                        criticalUpdateInfo=None, resolveSource = None,
                        updateJob = None, exactFlavors = False):
        """Create an update job. DEPRECATED, use newUpdateJob and
        prepareUpdateJob instead"""
        # FIXME: this API has gotten far out of hand.  Refactor when 
        # non backwards compatible API changes are acceptable. 
        # In particular. installMissing and updateOnly have similar meanings,
        # (but expanding updateOnly meaning would require making incompatible
        # changes), split has lost meaning, keepExisting is also practically 
        # meaningless at this level.
        # CNY-492
        assert(split)
        if keepRequired is None:
            keepRequired = self.cfg.keepRequired

        # To go away eventually
        if callback:
            import warnings
            warnings.warn("The callback argument to updateChangeSet has been "
                          "deprecated, use setUpdateCallback() instead")
            self.setUpdateCallback(callback)

        if self.updateCallback is None:
            self.setUpdateCallback(UpdateCallback())
        self.updateCallback.preparingChangeSet()

        if criticalUpdateInfo is None:
            criticalUpdateInfo = CriticalUpdateInfo()

        if updateJob:
            uJob = updateJob
        else:
            uJob = database.UpdateJob(self.db)

        hasCriticalUpdateInfo = False
        troveSource = uJob.getTroveSource()
        first = True
        for changeSet, incFConts in criticalUpdateInfo.iterChangeSets():
            if first:
                # Replace the trove source with one that can store
                # dependencies
                troveSource = trovesource.ChangesetFilesTroveSource(self.db,
                                                             storeDeps=True)
                uJob.troveSource = troveSource
                first = False
            troveSource.addChangeSet(changeSet, includesFileContents = incFConts)
            hasCriticalUpdateInfo = True

        forceJobClosure = False

        useAffinity = False
        if fromChangesets:
            # when --from-file is used we need to explicitly compute the
            # transitive closure for our job. we normally trust the 
            # repository to give us the right thing, but that won't
            # work when we're pulling jobs out of the change set
            forceJobClosure = True

            csSource = trovesource.ChangesetFilesTroveSource(self.db,
                                                             storeDeps=True)
            for cs in fromChangesets:
                self._replaceIncomplete(cs, self.db, self.db, self.repos)
                csSource.addChangeSet(cs, includesFileContents = True)
                # FIXME ChangeSetSource: We shouldn't have to add this to 
                # uJob.troveSource() at this point, since the 
                # changeset is not part of the job yet.  But given the 
                # way changeSetSource.createChangeSet is written
                # (it can't handle recursive changeSet creation, e.g.)
                # we have no choice.  Search FIXME ChangeSetSource for 
                # a matching comment
                uJob.getTroveSource().addChangeSet(cs,
                                                   includesFileContents = True)
        mainSearchSource = None
        troveSource = None
        searchSource = None
        if sync:
            troveSource = trovesource.ReferencedTrovesSource(self.db)
        elif syncChildren:
            troveSource = self.db
        elif fromChangesets:
            troveSource = trovesource.stack(csSource, self.repos)
            mainSearchSource = self.getSearchSource(troveSource=troveSource)
            searchSource = mainSearchSource
        elif hasCriticalUpdateInfo:
            # Use the trove source as a search source too
            searchSource = uJob.getTroveSource()
        else:
            mainSearchSource = self.getSearchSource()
            searchSource = mainSearchSource
            uJob.setSearchSource(mainSearchSource)
            useAffinity = True

        if not searchSource and troveSource:
            searchSource = searchsource.SearchSource(troveSource,
                                                     self.cfg.flavor)
        uJob.setSearchSource(searchSource)

        if resolveGroupList:
            if not mainSearchSource:
                mainSearchSource = self.getSearchSource()
            result = mainSearchSource.findTroves(resolveGroupList,
                                                 useAffinity=useAffinity,
                                                 exactFlavors=exactFlavors)
            groupTups = list(itertools.chain(*result.itervalues()))
            groupTroves = self.repos.getTroves(groupTups, withFiles=False)
            resolveSource = resolve.DepResolutionByTroveList(self.cfg, self.db,
                                                             groupTroves)
        if resolveSource:
            resolveRepos = False

        if migrate:
            jobSet = self._fullMigrate(itemList, uJob, recurse=recurse)
        else:
            jobSet = self._updateChangeSet(itemList, uJob,
                                       keepExisting = keepExisting,
                                       recurse = recurse,
                                       updateMode = updateByDefault,
                                       useAffinity = useAffinity,
                                       checkPrimaryPins = checkPrimaryPins,
                                       forceJobClosure = forceJobClosure,
                                       syncChildren = syncChildren,
                                       updateOnly = updateOnly,
                                       installMissing = installMissing,
                                       removeNotByDefault = removeNotByDefault,
                                       exactFlavors = exactFlavors)

        self._validateJob(jobSet)

        updateThreshold = self.cfg.updateThreshold

        # When keep existing is provided none of the changesets should
        # be relative (since relative change sets, by definition, cause
        # something on the system to get replaced).
        if keepExisting:
            for job in jobSet:
                if job[1][0] is not None:
                    raise UpdateError, 'keepExisting specified for a ' \
                                       'relative change set'

        self.updateCallback.resolvingDependencies()

        # this updates jobSet w/ resolutions, and splitJob reflects the
        # jobs in the updated jobSet
        (depList, suggMap, cannotResolve, splitJob, keepList, 
         criticalUpdates) = \
        info = self._resolveDependencies(uJob, jobSet, split = split,
                                      resolveDeps = resolveDeps,
                                      useRepos = resolveRepos,
                                      resolveSource = resolveSource,
                                      keepRequired = keepRequired,
                                      criticalUpdateInfo = criticalUpdateInfo)
        if keepList:
            self.updateCallback.done()
            for job, depSet, reqInfo in sorted(keepList):
                self.updateCallback.warning('keeping %s - required by at least %s',
                            job[0], reqInfo[0])

        if depList:
            raise DepResolutionFailure(self.cfg, depList, suggMap,
                                       cannotResolve, splitJob, criticalUpdates)
        elif suggMap and not self.cfg.autoResolve:
            raise NeededTrovesFailure(self.cfg, depList, suggMap,
                                      cannotResolve, splitJob, criticalUpdates)
        elif cannotResolve:
            raise EraseDepFailure(self.cfg, depList, suggMap,
                                  cannotResolve, splitJob, criticalUpdates)

        # look for troves which look like they'll conflict (same name/branch
        # and incompatible install paths)
        if not sync and checkPathConflicts:
            d = {}
            conflicts = {}
            for job in jobSet:
                if not job[2][0]: continue
                name, branch = job[0], job[2][0].branch()
                l = d.setdefault((name, branch), [])
                l.append(job)

            for jobList in d.values():
                if len(jobList) < 2: continue
                trvs = uJob.getTroveSource().getTroves(
                        [ (x[0], x[2][0], x[2][1]) for x in jobList ],
                        withFiles = False)
                paths = [ x.getPathHashes() for x in trvs ]

                for i, job in enumerate(jobList):
                    for j in range(i):
                        if not paths[i].compatibleWith(paths[j]):
                            l = conflicts.setdefault(job[0], [])
                            l.append((job[2], jobList[j][2]))

            if conflicts:
                raise InstallPathConflicts(conflicts)

        if criticalUpdates:
            criticalJobs = [ splitJob[x] for x in criticalUpdates ]
        else:
            criticalJobs = []

        finalCriticalJobs = []

        startNew = True
        newJob = []
        for jobList in splitJob:
            if startNew:
                newJob = []
                startNew = False
                count = 0
                newJobIsInfo = False
                inGroup = False

            isCritical = jobList in criticalJobs


            foundCollection = False
            foundGroup = False

            count += len(jobList)
            isInfo = None                 # neither true nor false
            infoName = None
            for job in jobList:
                (name, (oldVersion, oldFlavor),
                       (newVersion, newFlavor), absolute) = job

                if name.startswith('group-'):
                    foundGroup = True
                elif newVersion is not None and ':' not in name:
                    foundCollection = True

                if name.startswith('info-'):
                    assert(isInfo is True or isInfo is None)
                    isInfo = True
                    if not infoName:
                        infoName = name.split(':')[0]
                else:
                    assert(isInfo is False or isInfo is None)
                    isInfo = False

            if (((not isInfo or infoName != name) and newJobIsInfo is True)
                or foundGroup != inGroup):
                # We switched from installing info components to
                # installing fresh components. This has to go into
                # a separate job from the last one.
                # FIXME: We also require currently that each info 
                # job be for the same info trove - that is, can't
                # have info-foo and info-bar in the same update job
                # because info-foo might depend on info-bar being
                # installed already.  This should be fixed.
                if newJob:
                    uJob.addJob(newJob)
                count = len(jobList)
                newJob = list(jobList)             # make a copy
                newJobIsInfo = False
                inGroup = foundGroup
            else:
                newJobIsInfo = isInfo
                newJob += jobList

            if (foundCollection or isCritical
                or (updateThreshold and (count >= updateThreshold))): 
                if isCritical:
                    finalCriticalJobs.append(len(uJob.getJobs()))
                uJob.addJob(newJob)
                startNew = True

        if not startNew:
            # we don't care if the final job is critical - there 
            # will be no need for a restart in that case.
            #if isCritical:
            #    finalCriticalJobs.append(len(uJob.getJobs()))
            uJob.addJob(newJob)
        uJob.setCriticalJobs(finalCriticalJobs)

        uJob.setTransactionCounter(self.db.getTransactionCounter())

        # Save some misc information that could be useful to recreate the
        # update job

        kwargs = dict(
            keepExisting = keepExisting,
            recurse = recurse,
            resolveDeps = resolveDeps,
            test = test,
            updateByDefault = updateByDefault,
            split = split,
            sync = sync,
            checkPathConflicts = checkPathConflicts,
            checkPrimaryPins = checkPrimaryPins,
            resolveRepos = resolveRepos,
            syncChildren = syncChildren,
            updateOnly = updateOnly,
            resolveGroupList = resolveGroupList,
            installMissing = installMissing,
            removeNotByDefault = removeNotByDefault,
            keepRequired = keepRequired,
            migrate = migrate,
            exactFlavors = False)
        # Make sure we store them as booleans
        kwargs = dict( (k, bool(v)) for k, v in kwargs.iteritems())
        uJob.setItemList(itemList)
        uJob.setKeywordArguments(kwargs)
        uJob.setFromChangesets(fromChangesets)

        return (uJob, suggMap)

    def _validateJob(self, jobSet):
        # sanity check for jobSet - never allow a job that would add 
        # or remove the same trove twice to be applied to the system.
        oldTroves = [ (x[0], x[1]) for x in jobSet if x[1][0]]
        if not len(oldTroves) == len(set(oldTroves)):
            extraTroves = set(x for x in oldTroves if oldTroves.count(x) > 1)
            raise InternalConaryError(
                            "Update tries to remove same trove twice:\n    "
                              + '\n    '.join('%s=%s[%s]' % ((x[0],) + x[1]) 
                                              for x in sorted(extraTroves)))

        newTroves = [ (x[0], x[2]) for x in jobSet if x[2][0]]
        if not len(newTroves) == len(set(newTroves)):
            extraTroves = [ x for x in newTroves if newTroves.count(x) > 1 ]
            raise InternalConaryError(
                             "Update tries to add same trove twice:\n    "
                              + '\n    '.join('%s=%s[%s]' % ((x[0],) + x[1]) 
                                              for x in sorted(extraTroves)))

    def _createCs(self, repos, db, jobSet, uJob):
        baseCs = changeset.ReadOnlyChangeSet()

        cs, remainder = uJob.getTroveSource().createChangeSet(jobSet,
                                    recurse = False, withFiles = True,
                                    withFileContents = True,
                                    useDatabase = False)
        baseCs.merge(cs)
        if remainder:
            newCs = repos.createChangeSet(remainder, recurse = False,
                                          callback = self.updateCallback)
            baseCs.merge(newCs)

        self._replaceIncomplete(baseCs, db, db, repos)

        return baseCs

    def _applyCs(self, cs, uJob, removeHints = {}, **kwargs):
        # Before applying this job, reset the underlying changesets. This
        # lets us traverse user-supplied changesets multiple times.
        uJob.troveSource.reset()

        try:
            self.db.commitChangeSet(cs, uJob, callback=self.updateCallback, 
                                    **kwargs)
        except Exception, e:
            # an exception happened, clean up
            rb = uJob.getRollback()
            if rb:
                # remove the last entry from this rollback set
                # (which is the rollback entriy that roll back
                # applying this changeset)
                rb.removeLast()
                # if there aren't any entries left in the rollback,
                # remove it altogether, unless we're about to try again
                if (rb.getCount() == 0):
                    self.db.removeLastRollback()
            # rollback the current transaction
            self.db.db.rollback()
            if isinstance(e, database.CommitError):
                raise UpdateError, "changeset cannot be applied:\n%s" % e
            raise

    def _createAllCs(self, q, allJobs, uJob, cfg, stopSelf):
        # Reopen the local database so we don't share a sqlite object
        # with the main thread. This gets the user map from the already
        # existing repository object to ensure we still have access to
        # any passwords we need.
        # _createCs accesses the database through the uJob.troveSource,
        # so make sure that references this fresh db as well.
        import Queue

        db = database.Database(cfg.root, cfg.dbPath)
        uJob.troveSource.db = db
        repos = self.createRepos(db, cfg)
        self.updateCallback.setAbortEvent(stopSelf)

        for i, job in enumerate(allJobs):
            if stopSelf.isSet():
                return

            self.updateCallback.setChangesetHunk(i + 1, len(allJobs))
            try:
                newCs = self._createCs(repos, db, job, uJob)
            except:
                q.put((True, sys.exc_info()))
                return

            while True:
                # block for no more than 5 seconds so we can
                # check to see if we should abort
                try:
                    q.put((False, newCs), True, 5)
                    break
                except Queue.Full:
                    # if the queue is full, check to see if the
                    # other thread wants to quit
                    if stopSelf.isSet():
                        return

        self.updateCallback.setAbortEvent(None)
        q.put(None)

        # returning terminates the thread

    def getDownloadSizes(self, uJob):
        allJobs = uJob.getJobs()
        flatJobs = [ x for x in itertools.chain(*allJobs) ]
        flatSizes = self.repos.getChangeSetSize(flatJobs)

        sizes = []
        for job in allJobs:
            sizes.append(sum(flatSizes[0:len(job)]))
            flatSizes = flatSizes[len(job):]

        return sizes

    def downloadUpdate(self, uJob, destDir):
        allJobs = uJob.getJobs()
        csFiles = []
        for i, job in enumerate(allJobs):
            self.updateCallback.setChangesetHunk(i + 1, len(allJobs))
            # Create the relative changeset
            newCs = self._createCs(self.repos, self.db, job, uJob)

            # Dump the changeset to disk
            path = os.path.join(destDir, "%04d.ccs" % i)
            newCs.writeToFile(path)
            csFiles.append(path)

        uJob.setJobsChangesetList(csFiles)
        # Set the search source to use the downloaded troves
        csSource = trovesource.ChangesetFilesTroveSource(self.db,
                                                         storeDeps=True)
        csSource.addChangeSets(
            (changeset.ChangeSetFromFile(self.lzCache.open(x))
                for x in csFiles),
            includesFileContents = True)
        uJob.setSearchSource(csSource)
        uJob.troveSource = csSource
        uJob.setChangesetsDownloaded(True)


    def applyUpdate(self, uJob, replaceFiles = False, tagScript = None, 
                    test = False, justDatabase = False, journal = None, 
                    callback = None, localRollbacks = False,
                    autoPinList = conarycfg.RegularExpressionList(),
                    keepJournal = False):
        """Apply an update job. DEPRECATED, use applyUpdateJob instead"""
        commitFlags = database.CommitChangeSetFlags(
            replaceManagedFiles = replaceFiles,
            replaceUnmanagedFiles = replaceFiles,
            replaceModifiedFiles = replaceFiles,
            replaceModifiedConfigFiles = replaceFiles,
            justDatabase = justDatabase,
            localRollbacks = localRollbacks,
            test = test, keepJournal = keepJournal)

        return self._applyUpdate(uJob, tagScript = tagScript,
                              journal = journal, autoPinList = autoPinList,
                              commitFlags = commitFlags)

    def _applyUpdate(self, *args, **kwargs):
        # Calls _applyUpdateL, but deals with locks too
        try:
            self.db.commitLock(True)
            return self._applyUpdateL(*args, **kwargs)
        finally:
            self.db.commitLock(False)
            self.db.close()

    def _applyUpdateL(self, uJob, tagScript = None, journal = None,
                     callback = None, autoPinList = None,
                     commitFlags = None):
        uJobTransactionCounter = uJob.getTransactionCounter()
        if uJobTransactionCounter is None:
            # Legacy applications
            import warnings
            warnings.warn("Update jobs without a transaction counter have "
                          "been deprecated, use setTransactionCounter()")
        elif uJobTransactionCounter != self.db.getTransactionCounter():
            # Normally, this should not happen, unless someone froze the
            # update job and are trying to reapply it after the state of the
            # database has changed
            raise InternalConaryError("Stale update job")

        # To go away eventually
        if callback:
            import warnings
            warnings.warn("The callback argument to applyUpdate has been "
                          "deprecated, use setUpdateCallback() instead")
            self.setUpdateCallback(callback)

        if self.updateCallback is None:
            self.setUpdateCallback(UpdateCallback())

        allJobs = uJob.getJobs()

        self._validateJob(list(itertools.chain(*allJobs)))

        # run preinstall scripts
        if not self.db.runPreScripts(uJob, callback = self.getUpdateCallback(),
                                     tagScript = tagScript,
                                     justDatabase = commitFlags.justDatabase):
            raise UpdateError('error: preupdate script failed')

        # Simplify arg passing a bit
        kwargs = dict(
            commitFlags=commitFlags, tagScript=tagScript,
            journal=journal, autoPinList=autoPinList)

        if len(allJobs) == 1 and not uJob.getChangesetsDownloaded():
            # this handles change sets which include change set files
            # if we have the job already downloaded, skip this
            self.updateCallback.setChangesetHunk(0, 0)
            newCs = self._createCs(self.repos, self.db, allJobs[0], uJob)
            self.updateCallback.setUpdateHunk(0, 0)
            self.updateCallback.setUpdateJob(allJobs[0])
            self._applyCs(newCs, uJob, **kwargs)
            self.updateCallback.updateDone()
            return

        # build a set of everything which is being removed
        removeHints = dict()
        for job in allJobs:
            # the None in this dict means that all files in this trove
            # should be overridden
            removeHints.update([ ((x[0], x[1][0], x[1][1]), None)
                                    for x in job if x[1][0] is not None ])

        if uJob.getChangesetsDownloaded() or not self.cfg.threaded:
            for i, job in enumerate(allJobs):
                self.updateCallback.setChangesetHunk(i + 1, len(allJobs))
                newCs = self._createCs(self.repos, self.db, job, uJob)
                self.updateCallback.setUpdateHunk(i + 1, len(allJobs))
                self.updateCallback.setUpdateJob(job)
                self._applyCs(newCs, uJob, removeHints = removeHints, **kwargs)
                self.updateCallback.updateDone()
            return

        import Queue
        from conary.lib.fixedthreading import Thread
        # turn up the thread verbosity if we're in --debug=lowlevel
        if log.getVerbosity() == log.LOWLEVEL:
            import threading
            threading._VERBOSE = True
        from threading import Event

        csQueue = Queue.Queue(5)
        stopDownloadEvent = Event()

        downloadThread = Thread(None, self._createAllCs,
                args = (csQueue, allJobs, uJob, self.cfg, stopDownloadEvent))
        downloadThread.start()

        try:
            i = 0
            while True:
                try:
                    # get the next changeset object from the
                    # download thread.  Block for 10 seconds max
                    newCs = csQueue.get(True, 10)
                except Queue.Empty:
                    if downloadThread.isAlive():
                        continue

                    raise UpdateError('error: download thread terminated'
                                      ' unexpectedly, cannot continue update')
                if newCs is None:
                    break
                # We expect a (boolean, value)
                isException, val = newCs
                if isException:
                    raise val[0], val[1], val[2]

                newCs = val
                i += 1
                self.updateCallback.setUpdateHunk(i, len(allJobs))
                self.updateCallback.setUpdateJob(allJobs[i - 1])
                self._applyCs(newCs, uJob, removeHints = removeHints,
                              **kwargs)
                self.updateCallback.updateDone()
                if self.updateCallback.cancelOperation():
                    break
        finally:
            stopDownloadEvent.set()
            # the download thread _should_ respond to the
            # stopDownloadEvent in ~5 seconds.
            downloadThread.join(20)

            if downloadThread.isAlive():
                self.updateCallback.warning('timeout waiting for '
                    'download thread to terminate -- closing '
                    'database and exiting')
                self.db.close()
                tb = sys.exc_info()[2]
                if tb:
                    tb = traceback.format_tb(tb)
                    self.updateCallback.warning('the following '
                        'traceback may be related:',
                        exc_text=''.join(tb))
                # this will kill the download thread as well
                os.kill(os.getpid(), 15)
            else:
                # DEBUGGING NOTE: if you need to debug update code not
                # related to threading, the easiest thing is to add 
                # 'threaded False' to your conary config.
                pass


class UpdateError(ClientError):
    """Base class for update errors"""
    def display(self):
        return str(self)

class UpdatePinnedTroveError(UpdateError):
    """An attempt to update/erase a pinned trove."""
    def __init__(self, pinnedTrove, newVersion=None):
        self.pinnedTrove = pinnedTrove
        self.newVersion = newVersion
        
    def __str__(self):
        name = self.pinnedTrove[0]
        if self.newVersion:
            return """\
Not removing old %s as part of update - it is pinned.
Therefore, the new version cannot be installed.

To upgrade %s, run:
conary unpin '%s=%s[%s]'
and then repeat your update command
""" % ((name, name) + self.pinnedTrove)
        else:
            return """\
Not erasing %s - it is pinned.

To erase this %s, run:
conary unpin '%s=%s[%s]'
conary erase '%s=%s[%s]'
""" % ((name, name) + self.pinnedTrove + self.pinnedTrove)

class NoNewTrovesError(UpdateError):
    def __str__(self):
        return "no new troves were found"

class DependencyFailure(UpdateError):
    """ Base class for dependency failures """
    def __init__(self, cfg, depList, suggMap, cannotResolve,
                 jobSets, criticalUpdates):
        self.cfg = cfg
        self.depList = depList
        self.suggMap = suggMap
        self.cannotResolve = cannotResolve
        self.jobSets = jobSets
        self.criticalUpdates = criticalUpdates
        self.errorMessage = self._initErrorMessage()

    def __str__(self):
        return self.errorMessage

    def setErrorMessage(self, errorMessage):
        self.errorMessage = errorMessage

    def getErrorMessage(self):
        return self.errorMessage

    def hasCriticalUpdates(self):
        return bool(self.criticalUpdates)

    def getCriticalUpdates(self):
        return self.criticalUpdates

    def getSuggestions(self):
        return self.suggMap

    def getDepList(self):
        return self.depList

    def getCannotResolve(self):
        return self.cannotResolve

    def getJobSets(self):
        return self.jobSets

    def formatVF(self, troveTup, showVersion=True):
        if self.cfg.fullVersions:
            version = troveTup[1]
        elif self.cfg.showLabels:
            version = '%s/%s' % (troveTup[1].branch().label(), 
                                 troveTup[1].trailingRevision())
        elif showVersion:
            version = troveTup[1].trailingRevision()
        else:
            version = ''

        if self.cfg.fullFlavors:
            flavor = '[%s]' % troveTup[2]
        else:
            flavor = ''
        return '%s%s' % (version, flavor)


    def formatNVF(self, troveTup, showVersion=True):
        if not self.cfg:
            return '%s=%s' % (troveTup[0], troveTup[1].trailingRevision())
        versionFlavor = self.formatVF(troveTup, showVersion=showVersion)
        if versionFlavor and versionFlavor[0] != '[':
            return '%s=%s' % (troveTup[0], versionFlavor)
        return '%s%s' % (troveTup[0], versionFlavor)

class DepResolutionFailure(DependencyFailure):
    """ Unable to resolve dependencies """

    def getFailures(self):
        return self.depList

    def _initErrorMessage(self):
        res = ["The following dependencies could not be resolved:"]
        for (troveInfo, depSet) in self.depList:
            res.append("    %s:\n\t%s" %  \
                       (self.formatNVF(troveInfo),
                        "\n\t".join(str(depSet).split("\n"))))
        return '\n'.join(res)

class EraseDepFailure(DepResolutionFailure):
    """ Unable to resolve dependencies due to erase """

    def getFailures(self):
        return self.cannotResolve

    def _initErrorMessage(self):
        res = []
        packagesByErase = {}
        packagesByInstall = {}
        for jobSet in self.jobSets:
            for job in jobSet:
                newInfo = job[0], job[2][0], job[2][1]
                oldInfo = job[0], job[1][0], job[1][1]
                if job[1][0]:
                    packagesByErase[oldInfo] = newInfo
                if job[2][0]:
                    packagesByInstall[newInfo] = oldInfo

        res.append('The following dependencies would no longer be met after this update:\n')
        for (reqBy, depSet, providedBy) in self.getFailures():
            requiredPackages = []
            providers = []
            for oldInfo in providedBy:
                newInfo = packagesByErase[oldInfo]
                if not newInfo[1]:
                    status = 'Erased'
                else:
                    status = 'Updated to %s' % self.formatVF(newInfo)
                providedInfo = '%s (%s)' % (self.formatNVF(oldInfo), status)
                providers.append(providedInfo)
            if reqBy in packagesByInstall:
                oldInfo = packagesByInstall[reqBy]
                if oldInfo[1]:
                    reqByInfo = '%s (Updated from %s)' % (
                                            self.formatNVF(reqBy),
                                                self.formatVF(oldInfo))
                else:
                    reqByInfo = '%s (Newly Installed)' % self.formatNVF(reqBy)
            else:
                reqByInfo = '%s (Already Installed)' % self.formatNVF(reqBy)

            res.append("  %s requires:\n"
                       "    %s\n  which was provided by:\n"
                       "    %s" % (reqByInfo,
                                   "\n    ".join(str(depSet).split("\n")),
                               ' or '.join(providers)))
        return '\n'.join(res)

class NeededTrovesFailure(DependencyFailure):
    """ Dependencies needed and resolve wasn't used """

    def _initErrorMessage(self):
        res = []
        requiredBy = {}
        for (reqInfo, suggList) in self.suggMap.iteritems():
            for sugg in sorted(suggList):
               if sugg in requiredBy:
                    requiredBy[sugg].append(reqInfo)
               else:
                    requiredBy[sugg] = [reqInfo]
        numPackages = len(requiredBy)
        if numPackages == 1:
            res.append("%s additional trove is needed:" % numPackages)
        else:
            res.append("%s additional troves are needed:" % numPackages)
        for (suggInfo, reqList) in sorted(requiredBy.iteritems()):
            res.append("    %s is required by:" %  self.formatNVF(suggInfo))
            for reqInfo in sorted(reqList):
                res.append('       %s' % self.formatNVF(reqInfo))
        return '\n'.join(res)



class InstallPathConflicts(UpdateError):

    def __str__(self):
        res = []
        res.append("Troves being installed appear to conflict:")
        for name, l in sorted(self.conflicts.iteritems()):
            res.append("   %s -> %s" % (name, 
                           " ".join([ "%s[%s]->%s[%s]" %
                                        (x[0][0].asString(),
                                         deps.formatFlavor(x[0][1]),
                                         x[1][0].asString(),
                                         deps.formatFlavor(x[1][1]))
                                     for x in l ])))

        return '\n'.join(res)
    
    def __init__(self, conflicts):
        self.conflicts = conflicts

def _storeJobInfo(remainingJobs, updJob):
    changeSetSource = updJob.getTroveSource()
    restartDir = tempfile.mkdtemp(prefix='conary-restart-')
    csIndexPath = os.path.join(restartDir, 'changesets')
    csIndex = open(csIndexPath, "w")
    for idx, (cs, fname, incFConts) in enumerate(changeSetSource.iterChangeSetsFlags()):
        if isinstance(cs, changeset.ChangeSetFromFile):
            # Write the file name in the changesets file - when thawing we
            # will need this information
            csFileName = util.normpath(os.path.abspath(cs.fileName))
        else:
            cs.reset()
            csFileName = os.path.join(restartDir, '%d.ccs' % idx)
            cs.writeToFile(csFileName)
        csIndex.write("%s %s\n" % (csFileName, int(incFConts)))

    csIndex.close()

    jobSetPath = os.path.join(restartDir, 'joblist')
    jobFile = open(jobSetPath, 'w')
    jobStrs = []
    for job in itertools.chain(*remainingJobs): # flatten list
        jobStr = []
        if job[1][0]:
            jobStr.append('%s=%s[%s]--' % (job[0], job[1][0], job[1][1]))
        else:
            jobStr.append('%s=--' % (job[0],))
        if job[2][0]:
            jobStr.append('%s[%s]' % (job[2][0], job[2][1]))
        jobStrs.append(''.join(jobStr))
    jobFile.write('\n'.join(jobStrs))
    jobFile.close()
    # Write the version of the conary client
    # CNY-1034: we need to save more information about the currently running
    # client; upon restart, the new client may later check the old client's
    # version and recompute the update set if the old client was buggy.

    # Unfortunately, _loadRestartInfo will only ignore joblist, so we can't
    # drop a state file in the same restartDir. We'll create a new directory
    # and save the version file there.
    extraDir = restartDir + "misc"
    try:
        os.mkdir(extraDir)
    except OSError, e:
        # restartDir was a temporary directory, the likelyhood of extraDir
        # existing is close to zero
        # Just in case, remove the existing directory and re-create it
        util.rmtree(extraDir, ignore_errors=True)
        os.mkdir(extraDir)

    versionFilePath = os.path.join(extraDir, "__version__")
    versionFile = open(versionFilePath, "w+")
    versionFile.write("version %s\n" % constants.version)
    versionFile.close()

    # Save the version file in the regular directory too
    versionFilePath = os.path.join(restartDir, "__version__")
    versionFile = open(versionFilePath, "w+")
    versionFile.write("version %s\n" % constants.version)
    versionFile.close()

    # Save restart infromation
    invocationInfoPath = os.path.join(restartDir, "job-invocation")
    updJob.saveInvocationInfo(invocationInfoPath)

    return restartDir

def _loadRestartInfo(restartDir, lazyFileCache):
    changeSetList = []
    # Skip files that are not changesets (.ccs).
    # This was the first attempt to fix CNY-1034, but it would break
    # old clients.
    # Nevertheless the code now ignores everything but .ccs files

    # Value of dictionary is includesFileContents
    fileDict = dict((x, False) for x in os.listdir(restartDir) if x.endswith('.ccs'))
    # Add the changesets from the index file
    csIndexPath = os.path.join(restartDir, 'changesets')
    if os.path.exists(csIndexPath):
        for line in open(csIndexPath):
            cspath, includesFileContents = line.strip().split()[:2]
            includesFileContents = bool(int(includesFileContents))
            fileDict[cspath] = includesFileContents

    for path, includesFileContents in fileDict.iteritems():
        csFileName = os.path.join(restartDir, path)
        cs = changeset.ChangeSetFromFile(lazyFileCache.open(csFileName))
        changeSetList.append((cs, includesFileContents))
    jobSetPath = os.path.join(restartDir, 'joblist')
    jobSet = cmdline.parseChangeList(x.strip() for x in open(jobSetPath))
    finalJobSet = []
    for job in jobSet:
        if job[1][0]:
            oldVersion = versions.VersionFromString(job[1][0])
        else:
            oldVersion = None
        if job[2][0]:
            newVersion = versions.VersionFromString(job[2][0])
        else:
            newVersion = None
        finalJobSet.append((job[0], (oldVersion, job[1][1]), 
                            (newVersion, job[2][1]), job[3]))
    # If there was something to be done with the version information, it would
    # be performed by now. Clean up the misc directory
    util.rmtree(restartDir + "misc", ignore_errors=True)
    return finalJobSet, changeSetList

