###
 # Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
 # SPDX-License-Identifier: MIT-0
 #
 # Permission is hereby granted, free of charge, to any person obtaining a copy of this
 # software and associated documentation files (the "Software"), to deal in the Software
 # without restriction, including without limitation the rights to use, copy, modify,
 # merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
 # permit persons to whom the Software is furnished to do so.
 #
 # THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
 # INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A
 # PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
 # HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
 # OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
 # SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
 # Copyright Amazon.com, Inc. and its affiliates. All Rights Reserved.
#   SPDX-License-Identifier: MIT
import sys
import pandas as pd
import boto3
import re
import os
import json
from patternDetector import PatternDetector
from awsUtils import readTextFileFromS3, split_s3_path, getExtractedDataFromS3
import math
from datetime import datetime

pattern_detector = PatternDetector()

noLinesHeader = int(os.environ.get("NO_LINES_HEADER", 5))
noLinesFooter = int(os.environ.get("NO_LINES_FOOTER", 10))
filterParaWithWords = int(os.environ.get("FILTER_PARA_WORDS", 10))


def lambda_handler(event, context):
    paragraphs = []
    textractS3OutputPath = event['textract_result']['TextractTempOutputJsonPath']
    i = 1
    totalPages = event['numberOfPages']

    try:
        print("processing textract output in ", textractS3OutputPath)
        bucketName, prefixPath = split_s3_path(textractS3OutputPath)
        filterdHeaderFooter = pattern_detector.identifyHeaderFooterPattern(bucketName, prefixPath, totalPages)
        print("Header/Footer pattern found :", len(filterdHeaderFooter))
        page = 1
        while True:
            prefix = f"{prefixPath}/{i}"
            dataFiltered = getExtractedDataFromS3(bucketName, prefix)
            pages = dataFiltered['Page'].unique()
            pages.sort()
            for page in pages:
                data = dataFiltered[dataFiltered['Page'] == page]
                lRow = data.shape[0]
                if lRow == 0:
                    break
                page += 1
                pageParagraphs = parseTextractResponse(filterdHeaderFooter, data, i)
                if len(pageParagraphs) >= 1:
                    startLine = pageParagraphs[0]
                    if not startLine[0].isupper():
                        if findIncompletePharagraph(paragraphs, startLine):
                            pageParagraphs = pageParagraphs[1:]
                    paragraphs += pageParagraphs
            i += 1
    except Exception as e:
        print("end of processing :", i)
        print(e)

    filteredParagraphs = [p for p in paragraphs if len(p.split()) > filterParaWithWords]
    [print("{}\n====\n".format(p)) for p in filteredParagraphs]
    print(f"Number of paragraphs extracted: {len(paragraphs)}")
    print(f"Number of filtered paragraphs extracted: {len(filteredParagraphs)}")

    # Generate the output CSV filename
    original_filename = os.path.basename(textractS3OutputPath).replace('.json', '')
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    output_filename = f"{original_filename}-extracted-text-{timestamp}.csv"
    convertToCSVAndSave(output_filename, filteredParagraphs)

def findBin(bin):
    if bin < .4:
        return "0.1"
    elif bin < .8:
        return "0.5"
    else:
        return "0.9"

def combineParagraphs(pageParagraphs, paragraphs, paragraph):
    if len(paragraph) > 0:
        s = " ".join(paragraph)
        paragraphs.append(str(s))
    if len(paragraphs) >= 1:
        pageParagraphs += paragraphs
    return pageParagraphs

def parseTextractResponse(filterdHeaderFooter, dataFiltered, page):
    ptop = 0.0
    left = 0.0
    multiCols = []
    SKIP_PAGES = os.environ['SKIP_PAGES']
    skipPage = SKIP_PAGES.split(",")
    totalRow = dataFiltered.shape[0]
    pattern = re.compile(r'([A-Z]*[\.!?]$)', re.M)
    i = 0
    bins = {"0.1": {"paragraphs": [], "paragraph": [], "pTop": None, "pWidth": None, "pLeft": None},
            "0.5": {"paragraphs": [], "paragraph": [], "pTop": None, "pWidth": None, "pLeft": None},
            "0.9": {"paragraphs": [], "paragraph": [], "pTop": None, "pWidth": None, "pLeft": None}
            }
    for index, row in dataFiltered.iterrows():
        cTop = row["Geometry"]["BoundingBox"]["Top"]
        cLeft = row["Geometry"]["BoundingBox"]["Left"]
        cWidth = row["Geometry"]["BoundingBox"]["Width"]
        cHeight = row["Geometry"]["BoundingBox"]["Height"]
        totalRow -= 1
        txt = str(row['Text'])

        i += 1
        if i < noLinesHeader or totalRow < noLinesFooter:
            if filterdHeaderFooter.get(txt, 0) > 1:
                print("skipping header/footer row :", txt)
                continue
            if pattern_detector.regexHeaderOrFooter(txt):
                print("skipping based on pattern ", txt)
                continue

        bin = findBin(round((cLeft), 1))
        paragraphs = bins[bin]["paragraphs"]
        paragraph = bins[bin]["paragraph"]

        ptop = bins[bin]["pTop"]
        pWidth = bins[bin]["pWidth"]
        pLeft = bins[bin]["pLeft"]
        if ptop is None:
            ptop = cTop
            pWidth = cWidth
            pLeft = cLeft

        topDiff = cTop - ptop

        elemFound = [ele for ele in skipPage if (ele in txt)]
        if elemFound:
            print("skipping page :", txt)
            return []
        ltxt = len(txt)

        if topDiff > 0.02:
            if len(paragraph) >= 1:
                s = " ".join(paragraph)
                if not s[0].isupper():
                    if not findIncompletePharagraph(paragraphs, s):
                        paragraphs.append(str(s))
                else:
                    paragraphs.append(str(s))
            paragraph = []

        if ltxt >= 1:
            paragraph.append(txt)

        bins[bin]["pTop"] = cTop
        bins[bin]["pWidth"] = cWidth
        bins[bin]["pLeft"] = cLeft
        bins[bin]["paragraphs"] = paragraphs
        bins[bin]["paragraph"] = paragraph

    pageParagraphs = []
    for bin, section in bins.items():
        print("bins :", bin)
        pageParagraphs = combineParagraphs(pageParagraphs, section["paragraphs"], section["paragraph"])

    return pageParagraphs

def convertToCSVAndSave(textractS3OutputPath, paragraphs):
    csvdf = pd.DataFrame(paragraphs, columns=['content'])
    csvdf.to_csv(textractS3OutputPath, index=False)

def findIncompletePharagraph(paragraphs, line):
    lenP = len(paragraphs) - 1
    pattern = re.compile(r'([A-Z]*[\.!?]$)', re.M)
    while lenP >= 0:
        s = paragraphs[lenP]
        if len(pattern.findall(s)) == 0:
            paragraphs[lenP] = s + " " + line
            print("combinging string: ")
            return True
        lenP -= 1
    return False
