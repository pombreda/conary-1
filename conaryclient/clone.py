#
# Copyright (c) 2005 rPath, Inc.
#
# This program is distributed under the terms of the Common Public License,
# version 1.0. A copy of this license should have been distributed with this
# source file in a file called LICENSE. If it is not present, the license
# is always available at http://www.opensource.org/licenses/cpl.php.
#
# This program is distributed in the hope that it will be useful, but
# without any waranty; without even the implied warranty of merchantability
# or fitness for a particular purpose. See the Common Public License for
# full details.

import build
from build.nextversion import nextVersion
from deps import deps
import itertools
from lib import log
from repository import changeset
import versions

class ClientClone:

    def createClone(self, targetBranch, troveList = []):

        def _createSourceVersion(targetBranchVersionList, sourceVersion):
            assert(targetBranchVersionList)
            # sort oldest to newest
            targetBranchVersionList.sort()
            upstream = sourceVersion.trailingRevision().getVersion()

            # find the newest version in the list which shares the same version
            # as the new one we're going to commit (the list is sorted oldest
            # to newest)
            match = None
            for possibleVersion in targetBranchVersionList:
                if possibleVersion.trailingRevision().getVersion() == upstream:
                    match = possibleVersion

            if not match:
                match = targetBranchVersionList[0].branch().createVersion(
                            versions.Revision("%s-0" % 
                                sourceVersion.trailingRevision().getVersion()))

            match.incrementSourceCount()
                                 
            return match

        def _isUphill(ver, uphill):
            uphillBranch = uphill.branch()
            verBranch = ver.branch()
            if uphillBranch == verBranch:
                return True

            while verBranch.hasParentBranch():
                verBranch = verBranch.parentBranch()
                if uphillBranch == verBranch:
                    return True
            
            return False 

        def _isSibling(ver, possibleSibling):
            verBranch = ver.branch()
            sibBranch = possibleSibling.branch()

            verHasParent = verBranch.hasParentBranch()
            sibHasParent = sibBranch.hasParentBranch()

            if verHasParent and sibHasParent:
                return verBranch.parentBranch() == sibBranch.parentBranch()
            elif not verHasParent and not sibHasParent:
                # top level versions are always siblings
                return True

            return False

        def _createBinaryVersions(versionMap, leafMap, repos, srcVersion, 
                                  infoList):
            # this works on a single flavor at a time
            singleFlavor = list(set(x[2] for x in infoList))
            assert(len(singleFlavor) == 1)
            singleFlavor = singleFlavor[0]

            srcBranch = srcVersion.branch()

            infoVersionMap = dict(((x[0], x[2]), x[1]) for x in infoList)

            q = {}
            for name, cloneSourceVersion, flavor in infoList:
                q[name] = { srcBranch : [ flavor ] }

            currentVersions = repos.getTroveLeavesByBranch(q, bestFlavor = True)
            dupCheck = {}

            for name, versionDict in currentVersions.iteritems():
                lastVersion = versionDict.keys()[0]
                assert(len(versionDict[lastVersion]) == 1)
                assert(versionDict[lastVersion][0] == singleFlavor)
                leafMap[(name, infoVersionMap[name, singleFlavor], 
                         singleFlavor)] = (name, lastVersion, singleFlavor)
                if lastVersion.getSourceVersion() == srcVersion:
                    dupCheck[name] = lastVersion

            trvs = repos.getTroves([ (name, version, singleFlavor) for
                                        name, version in dupCheck.iteritems() ],
                                   withFiles = False)

            for trv in trvs:
                assert(trv.getFlavor() == singleFlavor)
                name = trv.getName()
                info = (name, trv.troveInfo.clonedFrom(), trv.getFlavor())
                if info in infoList:
                    # no need to reclone this one
                    infoList.remove(info)
                    versionMap[info] = trv.getVersion()

            if not infoList:
                return ([], None)

            buildVersion = nextVersion(repos, 
                                [ x[0] for x in infoList ], srcVersion, flavor)
            return infoList, buildVersion

        # get the transitive closure
        allTroveInfo = set()
        allTroves = dict()
        cloneSources = troveList
        while cloneSources:
            needed = []

            for info in cloneSources:
                if info[0].startswith("fileset"):
                    raise CloneError, "File sets cannot be cloned"
                    return
                elif info[0].startswith("group"):
                    raise CloneError, "Groups be cloned"

                if info not in allTroveInfo:
                    needed.append(info)
                    allTroveInfo.add(info)

            troves = self.repos.getTroves(needed, withFiles = False)
            allTroves.update(x for x in itertools.izip(needed, troves))
            cloneSources = [ x for x in itertools.chain(
                                *(t.iterTroveList() for t in troves)) ]

        # split out the binary and sources
        sourceTroveInfo = [ x for x in allTroveInfo 
                                    if x[0].endswith(':source') ]
        binaryTroveInfo = [ x for x in allTroveInfo 
                                    if not x[0].endswith(':source') ]

        versionMap = {}        # maps existing info to the version which is
                               # being cloned by this job, or where that version
                               # has already been cloned to
        leafMap = {}           # maps existing info to the info for the latest
                               # version of that trove on the target branch
        cloneJob = []          # (info, newVersion) tuples

        # start off by finding new version numbers for the sources
        for info in sourceTroveInfo:
            name, version = info[:2]

            try:
                currentVersionList = self.repos.getTroveVersionsByBranch(
                    { name : { targetBranch : None } } )[name].keys()
            except KeyError:
                currentVersionList = []

            if currentVersionList:
                currentVersionList.sort()
                leafMap[info] = (info[0], currentVersionList[-1], info[2])

                # if the latest version of the source trove was cloned from the
                # version being cloned, we don't need to reclone the source
                trv = self.repos.getTrove(name, currentVersionList[-1],
                                     deps.DependencySet(), withFiles = False)
                if trv.troveInfo.clonedFrom() == version:
                    versionMap[info] = trv.getVersion()
                else:
                    versionMap[info] = _createSourceVersion(currentVersionList, 
                                                            version)
                    cloneJob.append((info, versionMap[info]))
            else:
                newVersion = targetBranch.createVersion(
                    versions.Revision(
                        "%s-1" % version.trailingRevision().getVersion()))
                versionMap[info] = newVersion
                cloneJob.append((info, newVersion))

        # now go through the binaries; sort them into buckets based on the
        # source trove each came from. we can't clone troves which came
        # from multiple versions of the same source
        trovesBySource = {}
        for info in binaryTroveInfo:
            trv = allTroves[info]
            source = trv.getSourceName()
            # old troves don't have source info
            assert(source is not None)

            l = trovesBySource.setdefault(trv.getSourceName(), 
                                   (trv.getVersion().getSourceVersion(), []))
            if l[0] != trv.getVersion().getSourceVersion():
                log.error("Clone operation needs multiple versions of %s"
                            % trv.getSourceName())
            l[1].append(info)
            
        # this could be parallelized -- may not be worth the effort though
        for srcTroveName, (sourceVersion, infoList) in \
                                            trovesBySource.iteritems():
            newSourceVersion = versionMap.get(
                    (srcTroveName, sourceVersion, deps.DependencySet()), None)
            if newSourceVersion is None:
                # we're not cloning the source at the same time; try and fine
                # the source version which was used when the source was cloned
                try:
                    currentVersionList = self.repos.getTroveVersionsByBranch(
                      { srcTroveName : { targetBranch : None } } ) \
                                [srcTroveName].keys()
                except KeyError:
                    print "No versions of %s exist on branch %s." \
                                % (srcTroveName, targetBranch.asString()) 
                    return 1

                trv = self.repos.getTrove(srcTroveName, currentVersionList[-1],
                                     deps.DependencySet(), withFiles = False)
                if trv.troveInfo.clonedFrom() == sourceVersion:
                    newSourceVersion = trv.getVersion()
                else:
                    log.error("Cannot find cloned source for %s=%s" %
                                (srcTroveName, sourceVersion.asString()))
                    return 1

            # we know newSourceVersion is right at this point. now find the new
            # binary version for each flavor
            byFlavor = dict()
            for info in infoList:
                byFlavor.setdefault(info[2], []).append(info)

            for flavor, infoList in byFlavor.iteritems():
                cloneList, newBinaryVersion = \
                            _createBinaryVersions(versionMap, leafMap, 
                                                  self.repos, newSourceVersion, 
                                                  infoList)
                versionMap.update(
                    dict((x, newBinaryVersion) for x in cloneList))
                cloneJob += [ (x, newBinaryVersion) for x in cloneList ]
                
        # check versions
        for info, newVersion in cloneJob:
            if not _isUphill(info[1], newVersion) and \
                        not _isSibling(info[1], newVersion):
                log.error("clone only supports cloning troves to parent "
                          "and sibling branches")
                return 1

        if not cloneJob:
            log.warning("Nothing to clone!")
            return 1

        allTroves = self.repos.getTroves([ x[0] for x in cloneJob ])

        cs = changeset.ChangeSet()

        allFilesNeeded = list()

        for (info, newVersion), trv in itertools.izip(cloneJob, allTroves):
            newVersionHost = newVersion.branch().label().getHost()

            # if this is a clone of a clone, use the original clonedFrom value
            # so that all clones refer back to the source-of-all-clones trove
            if trv.troveInfo.clonedFrom() is None:
                trv.troveInfo.clonedFrom.set(trv.getVersion())

            oldVersion = trv.getVersion()
            trv.changeVersion(newVersion)

            # this loop only works for packages!
            for (name, version, flavor) in trv.iterTroveList():
                byDefault = trv.includeTroveByDefault(name, version, flavor)
                trv.delTrove(name, version, flavor, False)
                trv.addTrove(name, newVersion, flavor, byDefault = byDefault)

            uphillCache = {}
            needsNewVersions = []
            # discover which files need their versions changed (since we
            # don't want the cloned trove referring to files on the branch
            # which was the source of the clone)
            for (pathId, path, fileId, version) in trv.iterFileList():
                changeVersion = _isSibling(version, newVersion)
                if not changeVersion:
                    changeVersion = uphillCache.get(version, None)
                    if changeVersion is None:
                        changeVersion = _isUphill(version, newVersion)
                        uphillCache[version] = changeVersion

                if changeVersion:
                    needsNewVersions.append((pathId, path, fileId))

                if version.branch().label().getHost() != newVersionHost:
                    allFilesNeeded.append((pathId, fileId, version))

            # try and find the version on the target branch for files which
            # need to be reversioned
            if needsNewVersions:
                if info in leafMap:
                    oldTrv = self.repos.getTrove(withFiles = True, 
                                                 *leafMap[info])
                    map = dict(((x[0], x[2]), x[3]) for x in
                                            oldTrv.iterFileList())
                else:
                    map = {}

                for (pathId, path, fileId) in needsNewVersions:
                    ver = map.get((pathId, fileId), newVersion)
                    trv.updateFile(pathId, path, ver, fileId)

            # reset the signatures, because all the versions have now
            # changed, thus invalidating the old sha1 hash
            trv.troveInfo.sigs.reset()
            trvCs = trv.diff(None, absolute = True)[0]
            cs.newTrove(trvCs)

            if ":" not in trv.getName():
                cs.addPrimaryTrove(trv.getName(), trv.getVersion(), 
                                   trv.getFlavor())

        # the list(set()) removes duplicates
        newFilesNeeded = []
        for (pathId, newFileId, newFileVersion) in list(set(allFilesNeeded)):

            fileHost = newFileVersion.branch().label().getHost()
            if fileHost == newVersionHost:
                # the file is already present in the repository
                continue

            newFilesNeeded.append((pathId, newFileId, newFileVersion))

        fileObjs = self.repos.getFileVersions(newFilesNeeded)
        contentsNeeded = []
        pathIdsNeeded = []
        
        for (pathId, newFileId, newFileVersion), fileObj in \
                            itertools.izip(newFilesNeeded, fileObjs):
            (filecs, contentsHash) = changeset.fileChangeSet(pathId, None, 
                                                             fileObj)
            cs.addFile(None, newFileId, filecs)
            
            if fileObj.hasContents:
                contentsNeeded.append((newFileId, newFileVersion))
                pathIdsNeeded.append(pathId)

        contents = self.repos.getFileContents(contentsNeeded)
        for pathId, (fileId, fileVersion), fileCont, fileObj in \
                itertools.izip(pathIdsNeeded, contentsNeeded, contents, 
                               fileObjs):
            cs.addFileContents(pathId, changeset.ChangedFileTypes.file, 
                               fileCont, cfgFile = fileObj.flags.isConfig(), 
                               compressed = False)

        self.repos.commitChangeSet(cs)

class CloneError(Exception):

    pass