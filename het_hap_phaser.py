#!/usr/bin/env python3

import sys
import argparse
import re
import logging
import io
from collections import defaultdict
from parse_vcf import VcfReader, VcfHeader, VcfRecord 
from vase.ped_file import PedFile, Family, Individual, PedError
from vase.sample_filter import GtFilter
from vase.gnomad_filter import GnomadFilter 
from bedfinder import BedFinder

def parse_args():
    parser = argparse.ArgumentParser(
                       description='Output haplotypes in tabular format',
                       )
    required_args = parser.add_argument_group('Required Arguments')
    opt_args = parser.add_argument_group('Optional Arguments')
    #required arguments
    required_args.add_argument('-i', '--vcf', '--input', required=True, metavar='VCF', 
                                help='''Input VCF filename''')
    required_args.add_argument('-s', '--samples', required=True, nargs='+',
                                metavar='SAMPLE', 
                                help='''One or more samples to report 
                                        haplotypes for.''')
    required_args.add_argument('-p', '--ped', required=True, 
                                help='''PED file including sample and parental 
                                        IDs for at least each sample.''')
    required_args.add_argument('-v', '--variant', required=True, 
                                metavar='chr:pos-REF/ALT',
                                help='''Variant to report flanking haplotypes
                                        for. Must be in the format 
                                        "chr:pos-REF/ALT"''')

    opt_args.add_argument('-f', '--flanks', type=int, default=1000000,
                            help='''Distance (in bp) either side of --variant
                                    to report haplotypes of.''')
    opt_args.add_argument('--informative_only', action='store_true',
                            help='''Only output sites phased in at least one
                                    sample.''') 
    opt_args.add_argument('--phased_in_all', action='store_true',
                            help='''Only output sites phased in all 
                                    samples.''') 
    opt_args.add_argument('-m', '--max_no_calls', type=int, 
                            help='''Only output sites where the number of 
                                    uncalled or filtered genotypes in samples
                                    does not exceed this value.''')
    opt_args.add_argument('-a', '--output_alleles', action='store_true',
                            help='''Output one column per allele (i.e. two 
                                    columns per sample) rather than one column 
                                    per sample genotype.''') 
    opt_args.add_argument('-o', '--output',  
                            help='''Filename for tabular output.''')
    opt_args.add_argument('-g', '--gnomad_vcf',  nargs='+',
                            help='''gnomAD/ExAC VCFs for reporting allele 
                                    frequencies.''')
    opt_args.add_argument('-gnomad_pop', '--gnomad_pop', metavar='POP',
                          default='POPMAX',
                            help='''Report gnomAD/ExAC frequencies for this
                                    population. Default is to report POPMAX.
                          ''')
    opt_args.add_argument('-y', '--min_other_allele_freq', metavar='FREQ', 
                            type=float,
                            help='''Minimum gnomAD/ExAC allele frequency for 
                                    the non-haplotype allele at a site. If the 
                                    allele frequency of the non-haplotype 
                                    allele is greater than this value the 
                                    variant will be skipped. This can be used 
                                    to only output sites likely to be 
                                    informative when genotypes.''')
    opt_args.add_argument('-x', '--exclude_regions', metavar='BED',
                            help='''BED file of regions to ignore (e.g. repeat 
                                    masked regions).''')
    opt_args.add_argument('--gq', type=int, 
                            help='''Minimum genotype quality (GQ) score. 
                                    Genotype calls with lower GQs than this 
                                    will be treated as no-calls.''')
    opt_args.add_argument('--dp', type=int, 
                            help='''Minimum genotype depth. Genotype calls with
                                    lower depth than this value will be treated 
                                    as no-calls.''')
    opt_args.add_argument('--het_ab', type=float, 
                            help='''Minimum allele balance (0.0-0.5) for an ALT
                                    genotype in a heterozygous call. 
                                    Heterozygous calls with an ALT allele 
                                    balance lower than this value will be 
                                    treated as no-calls.''')
    opt_args.add_argument('--hom_ab', type=float, 
                            help='''Minimum allele balance (0.5-1.0) for an ALT
                                    genotype in a homozygous call. Homozygous 
                                    calls with an ALT allele balance lower than
                                    this value will be treated as no-calls.''')
    opt_args.add_argument('--quiet', action='store_true', 
                            help='''Do not output progress information to 
                                    STDERR.''')
    opt_args.add_argument('--debug', action='store_true', 
                            help='''Output debugging information to STDERR.''')
    return parser

def output_row(row, output, var, gnomad_readers, min_af, logger, samples, 
               gnomad_pop='POPMAX'):
    if gnomad_readers:
        logger.debug("Searching gnomad VCF for {}:{}-{}/{}".format(var.CHROM,
                                                                   var.POS,
                                                                   var.REF,
                                                                   var.ALT))
        max_af, max_popmax, max_pop = '.', '.', '.'
        for reader in gnomad_readers:
            af, popmax, pop, = search_gnomad(var, reader, gnomad_pop)
            if min_af is not None:
                if row[7] == len(samples): 
                    #if genotype does not refine region only output if above min_af
                    if af == '.': 
                        if row[5] == var.REF:
                            #not in gnomAD so presumably ALT AF is < min_af
                            logger.debug("Skipping variant due to absence in " +
                                         "gnomAD and haplotype is REF")
                            return 0
                        else:
                            other_af = 1.0
                    elif row[5] == var.REF: #haplotype allele is the REF allele
                        other_af = float(af)
                    else: #haplotype allele is the ALT allele
                        other_af = 1.0 - float(af)
                    if other_af < min_af:
                        logger.debug("Skipping variant due to gnomAD AF ({}) < {}"
                                     .format(other_af, min_af))
                        return 0
            if af != '.':
                if max_af == '.' or max_af < af:
                    max_af, max_popmax, max_pop = af, popmax, pop
        row.extend([max_af, max_popmax, max_pop])
    output.write(str.join("\t", (str(x) for x in row)) + "\n")
    return 1

def search_gnomad(var, gnomad_reader, pop='POPMAX'):
    hits = gnomad_reader.get_overlapping_records(var)
    for h in hits:
        for i in range(len(h.DECOMPOSED_ALLELES)):
            if var.DECOMPOSED_ALLELES[0] == h.DECOMPOSED_ALLELES[i]:
                af = h.INFO_FIELDS['AF'].split(',')[i]
                p_af = h.INFO_FIELDS['AF_'+ pop].split(',')[i]
                popmax = h.INFO_FIELDS['POPMAX'].split(',')[i]
                return (af, p_af, popmax)
    return ('.', '.', '.')

def parse_haplotypes(var, samples, unrelateds, ped_file, gt_filter, logger,
                     reverse_allele_order, output_alleles=False, 
                     index_var=False, ):
    ''' For given VcfRecord, return haplotypes for each sample and 
        summarise sample counts for remaining samples.
    '''
    alleles = [] #list of tuples (e.g. ("C", "T")) per sample
    calls = [] #list of strings (e.g. "C|T") per sample
    if len(var.ALLELES) != 2: #skip non-biallelic
        logger.debug("skipping non-biallelic variant at {}:{}"
                     .format(var.CHROM, var.POS))
        return None
    if len(var.ALLELES[0]) != len(var.ALLELES[1]):#skip indels
        logger.debug("skipping indel variant at {}:{}"
                     .format(var.CHROM, var.POS))
        return None
    counts = {"0/0": 0, "0/1": 0, "1/1": 0, }
    gts = var.parsed_gts(fields=gt_filter.fields)
    sample_with_alt = False
    allele_in_phase = dict()
    sample_no_calls = set()
    for s in samples:
        if not gt_filter.gt_is_ok(gts, s, 0) or not gt_filter.gt_is_ok(gts, s,
                                                                       1):
            sample_no_calls.add(s)
            calls.append("NoCall")
            alleles.append(("NoCall", "NoCall"))
            continue
        sgt = gts['GT'][s]
        if len(set(sgt)) == 1 and None in sgt:
            sample_no_calls.add(s)
            calls.append("NoCall")
            alleles.append(("NoCall", "NoCall"))
            continue
        if 1 in sgt:
            sample_with_alt = True
        if len(set(sgt)) == 1: #homozygous
            calls.append(str.join("|", (var.ALLELES[x] for x in sgt)))
            alleles.append( (var.ALLELES[sgt[0]], var.ALLELES[sgt[1]]) )
            allele_in_phase[s] = sgt[0]
            continue
        allele = max(sgt)
        fgt = ()
        mgt = ()
        f = ped_file.individuals[s].father
        m = ped_file.individuals[s].mother
        if f is not None:
            fgt = gts['GT'][f]
            if None in fgt or not gt_filter.gt_is_ok(gts, f, max(fgt)):
                fgt = ()
        if m is not None:
            mgt = gts['GT'][m]
            if None in mgt or not gt_filter.gt_is_ok(gts, m, max(mgt)):
                mgt = ()
        if not fgt and not mgt: #can't phase
            c = str.join("/", (var.ALLELES[x] for x in sgt))
            calls.append(c)
            alleles.append( (c,c) )
        else:
            pat = None
            mat = None
            if 1 in fgt:
                if 0 not in fgt or 1 not in mgt and mgt:#allele 1 inherited from father
                    pat = 1
            elif fgt:
                pat = 0
            if 1 in mgt:
                if 0 not in mgt or 1 not in fgt and fgt:#allele 1 inherited from mother
                    mat = 1
            elif mgt:
                mat = 0
            if mat is None and pat is None:#can't phase
                c = str.join("/", (var.ALLELES[x] for x in sgt))
                calls.append(c)
                alleles.append( (c,c) )
                continue
            elif mat is None:
                mat = int(pat == 0)
            elif pat is None:
                pat = int(mat == 0)
            if mat == pat:
                logger.warning("Apparent de novo variant in {} ".format(s) + 
                                "for {}:{}-{}/{}".format(var.CHROM, var.POS,
                                                         var.REF, var.ALT))
                c = str.join("/", (var.ALLELES[x] for x in sgt))
                calls.append(c)
                alleles.append( (c,c) )
                continue
            if index_var:
                if mat == 1 and pat == 0:
                    reverse_allele_order[s] = True
            if reverse_allele_order[s]:
                allele_in_phase[s] = mat
                calls.append("{}|{}".format(var.ALLELES[mat], var.ALLELES[pat]))
                alleles.append( (var.ALLELES[mat], var.ALLELES[pat]) )
            else:
                allele_in_phase[s] = pat
                calls.append("{}|{}".format(var.ALLELES[pat], var.ALLELES[mat]))
                alleles.append( (var.ALLELES[pat], var.ALLELES[mat]) )
    if not sample_with_alt:
        logger.debug("skipping variant without ALT allele in samples at {}:{}"
                     .format(var.CHROM, var.POS))
        return None
    for u in unrelateds:
        if (None not in gts['GT'][u] and 
            gt_filter.gt_is_ok(gts, u, max(gts['GT'][u]))):
            #genotype is called and passes GQ/DP/AB criteria
            counts[str.join("/", (str(x) for x in sorted(gts['GT'][u])))] += 1
    phased = list(allele_in_phase.values())
    n_in_phase = 0
    n_compat = 0
    p_allele = None
    if phased:
        ref_phase = phased.count(0)
        alt_phase = len(phased) - ref_phase
        if ref_phase > alt_phase:
            p_allele = 0
        elif alt_phase > ref_phase:
            p_allele = 1
        n_in_phase = max(ref_phase, alt_phase)
        n_compat = n_in_phase
    for s in (x for x in samples if x not in allele_in_phase):
        #check for compatible gts in unphased samples
        if p_allele is not None:
            if p_allele in gts['GT'][s] or s in sample_no_calls:
                n_compat += 1
    row = [var.CHROM, var.POS, var.ID, var.REF, var.ALT]
    if n_in_phase and p_allele is not None:
        row.append(var.ALLELES[p_allele])
    else:
        row.append('?')
    row.append(n_in_phase)
    row.append(n_compat)
    if output_alleles:
        for al in alleles:
            row.extend(al)
    else:
        row.extend(calls)
    an = 2 * sum(counts.values())
    ac = counts["0/1"] + (2*counts["1/1"])
    af = 0
    if an:
        af = ac/an
    row.extend([an, af])
    #append final item indicating whether calls are informative or not
    if phased: #at least one sample was phased successfully
        row.append(True)
    else: 
        row.append(False)
    if len(phased) == len(samples): #all samples were phased
        row.append(True)
    else: 
        row.append(False)
    row.append(len(sample_no_calls))
    return row

def vcf_to_hap(vcf, samples, ped, variant, flanks=1e6, output=None, 
               gnomad_vcf=[], gq=0, dp=0, het_ab=0., hom_ab=0., 
               informative_only=False, phased_in_all=False, max_no_calls=None,
               output_alleles=False, quiet=False, debug=False,
               min_other_allele_freq=None, exclude_regions=None, 
               gnomad_pop='POPMAX'):
    ''' 
        Find biallelics SNVs either side of variant and output a 
        haplotype table listing haplotypes in each sample.
        
        Args:
            vcf:    input VCF

            samples:
                    List of samples to report haplotypes for.

            ped:    PED file indicating at least relationships of each 
                    sample of interest.

            variant:
                    The variant to report flanking haplotypes for. Must 
                    be in the format "chr:pos-REF/ALT".

            flanks: Distance (in bp) either side of variant to report 
                    haplotypes for. Default = 1e6.

            output: Optional name for output file. Will print to STDOUT
                    by default.

            gq:     Minimum genotype quality (GQ) score. Genotype calls
                    with lower GQs than this will be treated as 
                    no-calls.

            dp:     Minimum genotype depth. Genotype calls with lower 
                    depth than this value will be treated as no-calls.

            het_ab: Minimum allele balance (0.0-0.5) for an ALT genotype 
                    in a heterozygous call. Heterozygous calls with an 
                    ALT allele balance lower than this value will be 
                    treated as no-calls.

            hom_ab: Minimum allele balance (0.5-1.0) for an ALT genotype 
                    in a homozygous call. Homozygous calls with an 
                    ALT allele balance lower than this value will be 
                    treated as no-calls.

            gnomad_vcf:
                    Optional gnomAD/ExAC VCF for reporting allele 
                    frequencies.
    
            gnomad_pop:
                    Optional gnomAD/ExAC population to report AF from. Default
                    is to always report POPMAX.

            informative_only:
                    Only output variants phased in at least one sample.

            phased_in_all:
                    Only output variants phased in ALL samples.

    '''
    logger = get_logger(debug, quiet)
    vreader = VcfReader(vcf)
    ped_file = PedFile(ped)
    v_chrom, v_pos, v_ref, v_alt = parse_var_string(variant)
    v_pos = int(v_pos)
    out_fh = get_output(output)
    gnomad_readers = get_gnomad_readers(gnomad_vcf)
    families = set()
    unrelateds = []
    reverse_allele_order = defaultdict(bool)
    if exclude_regions is not None:
        bfinder = BedFinder(exclude_regions)
    else:
        bfinder = None
    for s in samples:
        if s not in vreader.header.samples:
            raise RuntimeError("ERROR: Sample '{}' is not in VCF file!"
                               .format(s))
        if s not in ped_file.individuals:
            raise RuntimeError("ERROR: Sample '{}' is not in PED file!"
                               .format(s))
        families.add(ped_file.individuals[s].fid)
    for s in vreader.header.samples:
        if s not in ped_file.individuals:
            unrelateds.append(s)
        elif ped_file.individuals[s].fid not in families:
            unrelateds.append(s)
            families.add(ped_file.individuals[s].fid)
    gt_filter = GtFilter(vreader, gq=gq, dp=dp, het_ab=het_ab, hom_ab=hom_ab)
    logger.info("Searching input VCF for {}".format(variant))
    index_var = search_var(vreader, v_chrom, v_pos, v_ref, v_alt)
    if len(index_var.ALLELES) != 2:
        raise RuntimeError("ERROR: Can only parse biallelic variants and " + 
                           "index variant has {} alleles".format(
                                                       len(index_var.ALLELES)))
    index_row = parse_haplotypes(index_var, samples, unrelateds,  ped_file, 
                                 gt_filter, logger, reverse_allele_order, 
                                 index_var=True)
    index_row[2] += "|INDEX"
    index_row.pop() # remove N NoCalls
    index_row.pop() # remove phased in all flag
    index_row.pop() # remove informative only flag
    out_fh.write('#' + str.join(" ", sys.argv) + "\n")
    if output_alleles:
        sample_cols = [x + "_Allele_1\t" + x + "_Allele_2"  for x in samples] 
    else:
        sample_cols = samples 
    header_cols = ["#CHROM", "POS", "ID", "REF", "ALT", "HAP", "N_IN_PHASE", 
                   "N_COMPAT"] + sample_cols + ["OTHER_N_ALLELES", "ALT_MAF",]
    if gnomad_readers:
        header_cols.extend(["gnomAD_AF", "gnomAD_AF_POPMAX", "POPMAX_pop"])
    out_fh.write(str.join("\t", header_cols) + "\n")
    start = int(v_pos) - flanks if flanks < v_pos else 1
    end   = int(v_pos)  - 1
    parse_region(vreader, samples, unrelateds, ped_file, v_chrom, start, end, 
                 out_fh, gt_filter, logger, reverse_allele_order, 
                 informative_only, phased_in_all, avoid_bed=bfinder,
                 output_alleles=output_alleles, max_no_calls=max_no_calls, 
                 gnomad_readers=gnomad_readers, 
                 min_other_allele_freq=min_other_allele_freq,
                 gnomad_pop=gnomad_pop)
    _foo = output_row(index_row, out_fh, index_var, gnomad_readers, None, 
                      logger, None, gnomad_pop)
    start = int(v_pos) + 1
    end   = int(v_pos) + flanks
    parse_region(vreader, samples, unrelateds, ped_file, v_chrom, start, end, 
                 out_fh, gt_filter, logger, reverse_allele_order, 
                 informative_only, phased_in_all, avoid_bed=bfinder,
                 output_alleles=output_alleles, max_no_calls=max_no_calls, 
                 gnomad_readers=gnomad_readers, 
                 min_other_allele_freq=min_other_allele_freq, 
                 gnomad_pop=gnomad_pop)
    if output is not None:
        out_fh.close()

def parse_region(vcf, samples, unrelateds, ped_file, chrom, start, end, output, 
                 gt_filter, logger, reverse_alleles, informative_only, 
                 phased_in_all, output_alleles=False, max_no_calls=None, 
                 gnomad_readers=[], avoid_bed=None, 
                 min_other_allele_freq=None, gnomad_pop='POPMAX'):
    logger.info("Searching for variants in region {}:{:,}-{:,}".format(
                                                            chrom, start, end))
    vcf.set_region(chrom, start - 1, end)
    n = 0
    w = 0
    for var in vcf.parser:
        if n % 100 == 0 and n != 0:
            logger.info("Parsed {} variants, wrote {}, at {}:{}"
                        .format(n, w, var.CHROM, var.POS))
        if avoid_bed is not None:
            if avoid_bed.fetch(var.CHROM, var.POS, var.SPAN):
                n += 1
                logger.debug("Variant at {}:{:,} overlaps exclude BED - " + 
                             "skipping".format(var.CHROM, var.POS))
                continue
        row = parse_haplotypes(var, samples, unrelateds, ped_file, gt_filter, 
                               logger, reverse_alleles, output_alleles, False)
        if row is not None:
            no_calls = row.pop()
            all_phased = row.pop()
            informative = row.pop()
            if max_no_calls is not None and no_calls > max_no_calls:
                n += 1
                logger.debug("Skipping {}:{} due to too many no-calls"
                             .format(row[0], row[1]))
                continue
            if phased_in_all:
                if all_phased:
                    w += output_row(row, output, var, gnomad_readers, 
                               min_other_allele_freq, logger, samples, 
                               gnomad_pop)
                else:
                    logger.debug("Skipping {}:{} due to not all samples phased"
                                 .format(row[0], row[1]))
            elif informative_only:
                if informative:
                    w += output_row(row, output, var, gnomad_readers, 
                               min_other_allele_freq, logger, samples,
                               gnomad_pop)
                else:
                    logger.debug("Skipping {}:{} due to not informative"
                                 .format(row[0], row[1]))
            else:
                w += output_row(row, output, var, gnomad_readers,
                                min_other_allele_freq, logger, samples,
                                gnomad_pop)
        n += 1

def search_var(vcf, chrom, pos, ref, alt):
    span = pos + len(ref) - 1
    vcf.set_region(chrom, pos - 1, span)
    for var in vcf.parser:
        for i in range(len(var.DECOMPOSED_ALLELES)):
            if (var.DECOMPOSED_ALLELES[i].POS == pos and 
                var.DECOMPOSED_ALLELES[i].REF == ref and 
                var.DECOMPOSED_ALLELES[i].ALT == alt):
                return var
    raise RuntimeError("Could not find matching variant for {}:{}-{}/{}"
                       .format(chrom, pos, ref, alt))

def get_logger(debug=False, quiet=False):
    logger = logging.getLogger("het_hap_phaser")
    if debug:
        logger.setLevel(logging.DEBUG)
    elif quiet:
        logger.setLevel(logging.WARNING)
    else:
        logger.setLevel(logging.INFO)
    formatter = logging.Formatter(
                    '[%(asctime)s] %(name)s - %(levelname)s - %(message)s')
    ch = logging.StreamHandler()
    ch.setLevel(logger.level)
    ch.setFormatter(formatter)
    logger.addHandler(ch)
    return logger

def get_gnomad_readers(vcfs):
    readers = []
    if vcfs is not None:
        for vcf in vcfs:
            readers.append(GnomadFilter(vcf, "gnomAD"))
    return readers

def get_output(output):
    ''' 
        Return an output filehandle. If no output specified return 
        sys.stdout.
    '''
    if isinstance(output, str):
        fh = open(output, 'w')
    else:
        fh = sys.stdout
    return fh

def parse_var_string(variant):
    v_re = re.compile(r"""^(\S+):(\d+)-(\S+)/(\S+)$""")
    autosome_re = re.compile(r"""^(chr)?(\d+)$""")
    match = v_re.match(variant)
    if match:
        if autosome_re.match(match.group(1)):
            return match.groups()
        else:
            raise RuntimeError("ERROR: Can only handle autosomal chroms - " + 
                     "variant {} ".format(variant) + "does not look like an " + 
                     "autosomal variant.")
    raise RuntimeError("ERROR: Could not parse variant '{}'".format(variant))
    
if __name__ == '__main__':
    parser = parse_args()
    args = parser.parse_args()
    vcf_to_hap(**vars(args))
 
