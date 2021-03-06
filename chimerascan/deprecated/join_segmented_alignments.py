'''
Created on Jan 7, 2011

@author: mkiyer

chimerascan: chimeric transcript discovery using RNA-seq

Copyright (C) 2011 Matthew Iyer

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
'''
import logging
import collections

# local imports
from chimerascan import pysam
from chimerascan.lib import config
from chimerascan.lib.base import SamTags
from fix_alignment_ordering import fix_segmented_alignment_ordering


def build_segment_alignment_dict(aln_dict, reads, seg_num):
    aln_seg_dict = {}
    for r in reads:
        if r.is_unmapped:
            continue
        #print 'READ', r        
        # check whether an adjacent segment exists to join with
        join_key = (r.rname, r.is_reverse, r.aend if r.is_reverse else r.pos)
        #print 'JOIN_KEY', join_key
        if join_key not in aln_dict:
            # make a new segment to join with
            join_pos = r.pos if r.is_reverse else r.aend
            #print 'NEW_ENTRY', r.rname, r.is_reverse, join_pos
            aln_seg_dict[(r.rname, r.is_reverse, join_pos)] = (r.rname, r.pos, r.aend, r.is_reverse, [seg_num], [r])
        else:
            # get old join segment
            rname, start, end, is_reverse, seg_inds, seg_reads = aln_dict[join_key]
            del aln_dict[join_key]
            # make a new entry in the aligment dict
            seg_inds.append(seg_num)
            seg_reads.append(r)
            if is_reverse:
                start = r.pos
                join_pos = start
            else:
                end = r.aend
                join_pos = end
            # add new join segment
            #print 'UPDATE_ENTRY', rname, is_reverse, join_pos
            aln_seg_dict[(rname, is_reverse, join_pos)] = (rname, start, end, is_reverse, seg_inds, seg_reads)
    # update main alignment dict with items from this segment
    aln_dict.update(aln_seg_dict)
 
def make_unmapped_copy(r):
    a = pysam.AlignedRead()
    a.qname = r.qname
    a.seq = r.seq
    a.qual = r.qual
    a.is_unmapped = True
    a.is_qcfail = False
    a.is_paired = True
    a.is_proper_pair = False
    a.mate_is_unmapped = True
    a.mrnm = -1
    a.mpos = -1
    a.is_read1 = r.is_read1
    a.is_read2 = r.is_read2
    a.isize = 0
    a.mapq = 255
    a.is_reverse = False
    a.rname = -1
    a.pos = 0
    a.cigar = ()
    a.tags = (('XM', 0),)
    return a

def get_contiguous_indexes(ind_list):
    if len(ind_list) == 0:
        return []
    contig_inds = []
    current_inds = [ind_list[0]]
    for ind in ind_list[1:]:
        if ind != current_inds[-1] + 1:
            contig_inds.append(tuple(current_inds))
            current_inds = []
        current_inds.append(ind)
    if len(current_inds) > 0:
        contig_inds.append(tuple(current_inds))
    return contig_inds


def find_valid_segment_alignments(read_mappings):
    # build a map of reference positions to read segment positions
    num_segs = len(read_mappings)
    aln_dict = collections.defaultdict(lambda: [None] * num_segs)
    unmapped_seg_reads = {}
    unmapped_inds = set()
    for seg_num, seg_mappings in enumerate(read_mappings):
        if len(seg_mappings) == 1 and seg_mappings[0].is_unmapped == True:
            # make an unmapped version of each segment to use in joining
            unmapped_seg_reads[seg_num] = seg_mappings[0]
            unmapped_inds.add(seg_num)
        else:
            unmapped_seg_reads[seg_num] = make_unmapped_copy(seg_mappings[0])
        build_segment_alignment_dict(aln_dict, seg_mappings, seg_num)
    # get the set of all segment indexes
    all_inds = set(range(num_segs))
    # get the set of mappable segment indexes
    mappable_inds = all_inds.difference(unmapped_inds)

    # if there are no mappings, then all the segments must be non-mapping
    # and must only have one entry at the 0th position in the segment
    # mapping array.  create an unmapped entry in this case.
    if len(aln_dict) == 0:
        return [[[[unmapped_seg_reads[i] for i in xrange(num_segs)]]]]

    # there are some segment mappings, but some of the segments could
    # still be unmapped.  here we create a list of joined sub-segments
    # that together comprise a full read by joining consecutive mapped
    # and unmapped segments
    # find the alignments where the maximum number of segments are
    # joined (the set of best alignments) by decorating the lists with
    # the number of mapped segments then using it as a sort key
    segment_dict = collections.defaultdict(lambda: [])
    for aln_key, aln_info in aln_dict.iteritems():
        (rname, start, end, is_reverse, seg_inds, seg_reads) = aln_info        
        segment_dict[tuple(seg_inds)].append(seg_reads)    
    sorted_mapping_inds = collections.deque(sorted(segment_dict.keys(), key=len, reverse=True))

    # build list of valid segment alignments prioritized by the
    # size of the largest contiguous segment (ties broken by 2nd largest,
    # 3rd largest, etc)
    best_rank = 0 
    rank = 0
    joined_segs_sets = set()
    while len(sorted_mapping_inds) > 0:
        joined_segs = set()
        used_inds = set()
        # iterate through sorted segment indices and add
        # independent segments so that all mappable segments
        # are part of the final joined read 
        for mapping_inds in sorted_mapping_inds:
            if used_inds.isdisjoint(mapping_inds):            
                used_inds.update(mapping_inds)        
                joined_segs.add(mapping_inds)
                if used_inds == mappable_inds:
                    break
        # remove best element from mapping index dict
        sorted_mapping_inds.popleft()
        # rank this segment to determine whether to 
        # keep looping
        rank = tuple(len(x) for x in joined_segs)
        if (best_rank == None) or (rank > best_rank):
            best_rank = rank
        elif (rank < best_rank):
            break
        # find missing indexes to fill with unmapped reads
        missing_inds = sorted(all_inds.difference(used_inds))
        contig_missing_inds = get_contiguous_indexes(missing_inds)
        for inds_tuple in contig_missing_inds:
            joined_segs.add(inds_tuple)
        # add to set of joined_segs
        if joined_segs not in joined_segs_sets: 
            joined_segs_sets.add(frozenset(joined_segs))

    # convert lists of joined segments to full reads
    joined_reads = []
    for joined_segs in joined_segs_sets:
        # extract reads at each segment
        split_reads = []
        # sort indexes in order of original read
        for seg_inds in sorted(joined_segs):
            if seg_inds in segment_dict:
                #print 'SEG INDS FOUND IN DICT', seg_inds
                seg_reads = segment_dict[seg_inds]
            else:
                #print 'UNMAPPED', seg_inds
                seg_reads = [[unmapped_seg_reads[i] for i in seg_inds]]
            #print 'SEG READS', seg_reads
            split_reads.append(seg_reads)
        joined_reads.append(split_reads)
    #print 'JOINED READS', joined_reads
    return joined_reads


def parse_MD_tag(val):
    x = 0
    mdops = []
    for y in xrange(len(val)):
        if val[y].isalpha():
            offset = int(val[x:y])
            base = val[y]
            mdops.append(offset)
            mdops.append(base)
            x = y + 1
    if x < len(val):
        mdops.append(int(val[x:]))
    return mdops

def merge_MD_tags(vals):
    mdops = parse_MD_tag(vals[0])
    for val in vals[1:]:
        nextops = parse_MD_tag(val)
        if isinstance(mdops[-1], int) and isinstance(nextops[0], int):
            mdops[-1] += nextops[0]
            nextops = nextops[1:]
        mdops.extend(nextops)
    return ''.join(map(str, mdops))
               
def make_joined_read(mate, reads, tags=None):
    if tags is None:
        tags = []
    # flip reverse strand reads
    if not reads[0].is_unmapped and reads[0].is_reverse:
        reads = sorted(reads, reverse=True)
    # make new reads
    a = pysam.AlignedRead()
    # create paired-end reads but do not mark them
    # as proper pairs and set all mate information
    # to 'unmapped'
    a.qname = reads[0].qname
    a.seq = ''.join(r.seq for r in reads)
    a.qual = ''.join(r.qual for r in reads)
    a.is_paired = True
    a.is_proper_pair = False
    a.mate_is_unmapped = True
    a.mrnm = -1
    a.mpos = -1
    if mate == 0:
        a.is_read1 = True
        a.is_read2 = False
    else:
        a.is_read1 = False
        a.is_read2 = True
    a.isize = 0
    a.mapq = 255
    a.is_unmapped = reads[0].is_unmapped
    if a.is_unmapped:
        a.rname = -1
        a.pos = 0
        # add the XM tag from bowtie saying whether unmapped
        # due to multimapping or other reason
        xm_tag = min(r.opt('XM') for r in reads)
        tags.append(('XM', xm_tag))
    else:
        a.is_reverse = reads[0].is_reverse
        a.rname = reads[0].rname
        a.pos = reads[0].pos
        a.cigar = ((0, len(a.seq)),)
        # compute edit dist
        edit_dist = 0
        for r in reads:
            edit_dist += r.opt('NM')
        tags.append(('NM', edit_dist))
        # compute mismatches to reference (MD)
        tags.append(('MD', merge_MD_tags([r.opt('MD') for r in reads])))
    a.tags = tags
    return a


# Reference Types
REF_GENOME = 0
REF_GENE = 1

def get_tid_ref_types(bamfh):
    tid_types = []
    for ref in bamfh.references:
        if ref.startswith(config.GENE_REF_PREFIX):
            tid_types.append(REF_GENE)
        else:
            tid_types.append(REF_GENOME)
    return tid_types

def join_segmented_alignments(input_sam_file, input_fastq_file, output_bam_file, is_paired):
    # setup debugging logging messages
    debug_count = 0
    debug_every = 1e6
    debug_next = debug_every
    # open sam file
    infh = pysam.Samfile(input_sam_file, "r")
    #header = infh.header
    outfh = pysam.Samfile(output_bam_file, "wb", template=infh)
    tid_type_map = get_tid_ref_types(outfh)
    #outfh = pysam.Samfile("-", "w", template=infh)
    # iterate through paired-end alignments
    logging.info("Processing paired alignments")
    align_iter = fix_segmented_alignment_ordering(infh, 
                                                  open(input_fastq_file), 
                                                  is_paired)    
    for segmented_pe_reads in align_iter: 
        debug_count += 1
        if debug_count == debug_next:
            debug_next += debug_every
            logging.debug("Processed %d reads" % debug_count)            
        # get alignments    
        for mate, mate_segs in enumerate(segmented_pe_reads):
            # search for segment matches
            joined_hits = find_valid_segment_alignments(mate_segs)
            num_hits = len(joined_hits)
            #print 'HITS', num_hits
            for hit_index, split_hits in enumerate(joined_hits):
                # total number of splits
                num_splits = len(split_hits)                
                #print 'HIT', hit_index, 'SPLITS', len(split_hits)
                for split_index, seg_hits in enumerate(split_hits):
                    num_seg_hits = len(seg_hits)
                    split_reads = []
                    multimaps = 0
                    #print 'SPLIT', split_index, 'HITS', num_seg_hits
                    for seg_index, seg_reads in enumerate(seg_hits):                        
                        # make SAM record for each segment
                        tags = [(SamTags.RTAG_NUM_PARTITIONS, num_hits),
                                (SamTags.RTAG_PARTITION_IND, hit_index),
                                (SamTags.RTAG_NUM_SPLITS, num_splits),
                                (SamTags.RTAG_SPLIT_IND, split_index),
                                (SamTags.RTAG_NUM_MAPPINGS, num_seg_hits),
                                (SamTags.RTAG_MAPPING_IND, seg_index)]                        
                        r = make_joined_read(mate, seg_reads, tags=tags)
                        split_reads.append(r)
                        # TODO: keep track of multimaps using the number of 
                        # genome hits as a proxy (this is not perfect, since 
                        # splice junction reads could be multimapping
                        if tid_type_map[r.rname] == REF_GENOME:
                            multimaps += 1
                    # output reads now that multimappings have been computed
                    for r in split_reads:
                        if not r.is_unmapped:
                            r.tags = r.tags + [("NH", multimaps)]                         
                        outfh.write(r)

if __name__ == '__main__':
    from optparse import OptionParser
    logging.basicConfig(level=logging.DEBUG,
                        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    parser = OptionParser("usage: %prog [options] <insam> <outsam>")
    parser.add_option("--sr", dest="sr", action="store_true", default=False)
    options, args = parser.parse_args()
    is_paired = not options.sr
    input_sam_file = args[0]
    input_fastq_file = args[1]
    output_bam_file = args[2]
    logging.debug("Joining segmented paired-end mappings")
    logging.debug("Input SAM file: %s" % (input_sam_file))
    logging.debug("Input FASTQ file: %s" % (input_fastq_file))
    logging.debug("Output BAM file: %s" % (output_bam_file))
    join_segmented_alignments(input_sam_file, input_fastq_file, output_bam_file, is_paired)
