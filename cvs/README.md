Drop your CV variant files here, named to match the CVVariant enum:

    CV_Energy.tex   CV_Quant.tex   CV_DataEng.tex
    CV_MLAI.tex     CV_Electrical.tex   CV_Consulting.tex

.tex, .md and .txt all work; LaTeX is stripped to plain text before it reaches
the prompt. Run `jobscan cvs` to check they parse.

Without these the scorer still runs, but tailoring advice stays generic because
the model cannot see what each variant actually says.
