#
# Copyright (c) 2004 Specifix, Inc.
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
#
from repository import changeset
from repository import repository
import filecontainer
import log
import versions

def doCommit(repos, changeSetFile, targetBranch):
    try:
	cs = changeset.ChangeSetFromFile(changeSetFile)
    except filecontainer.BadContainer:
	log.error("invalid changeset %s", changeSetFile)
	return 1

    if targetBranch:
	if cs.isAbsolute():
	    # we can't do this -- where would we branch from?
	    log.error("absolute change sets cannot be retargeted")
	    return
	label = versions.BranchName(targetBranch)
	cs.setTargetBranch(repos, label)

    if cs.isLocal():
	log.error("local change sets cannot be applied to a repository "
		  "without a branch override")

    try:
        if targetBranch:
            # XXX we currently cannot write out the retargeted changeset
            repos.commitChangeSet(cs)
        else:
            # hopefully the file hasn't changed underneath us since we
            # did the check at the top of doCommit().  We should probably
            # add commitChangeSet method that takes a fd.
            repos.commitChangeSetFile(changeSetFile)
    except repository.CommitError, e:
	print e
	
def doLocalCommit(db, changeSetFile):
    cs = changeset.ChangeSetFromFile(changeSetFile)
    if not cs.isLocal():
	log.error("repository changesets must be applied with update instead")
    db.commitChangeSet(cs, isRollback = True, toStash = False)
    

