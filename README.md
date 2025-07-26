[![SVG Banners](https://svg-banners.vercel.app/api?type=origin&text1=MAGIS%20🧑‍💻&text2=💖%20GitHub%20Issue%20Resolusion&width=900&height=200)](https://github.com/Akshay090/svg-banners)


# 👉🏻 MAGIS 👈🏻


📃 <a href="https://arxiv.org/abs/2403.17927" target="_blank">arXiv Paper</a> ·  📓 <a href="https://proceedings.neurips.cc/paper_files/paper/2024/hash/5d1f02132ef51602adf07000ca5b6138-Abstract-Conference.html" target="_blank">NeurIPS 2024</a>  ·  ❞ <a href="#-citation"> Citation </a>


## ✨ Key Features

* 🪶 **Simple LLM-based Multi-Agent Framework for Software Evolution**

* 🔍 **Visualization of Multi-Agent Processing**


## 📦 Environment

> All commands below have been tested on a Linux-64 machine. The specified package versions are recommended, as they have been verified to work.

```bash
conda create -n magis python=3.11 -y
conda activate magis
conda install redis==5.0.3 -y  # If you need to cache repeated requests to the same LLM
pip install openai==0.27.8 pygments==2.17.2 pydeps==1.12.17 tiktoken==0.5.1 redis==5.0.1 flask==3.0.0 datasets==2.16.1
```

## 🚀 Run

To use MAGIS on a Git repository, please run the [magis.py](magis.py) script.  
You can view options and usage instructions by executing: `python magis.py --help`.


To reproduce our experiments on [SWE-bench](https://github.com/SWE-bench/SWE-bench/tree/b93ac87), please refer to [SWE-bench.md](exp/SWE-bench/README.md).



## 📖 Citation

If you find [MAGIS](https://github.com/co-evolve-lab/magis) useful for your research or applications, feel free to give us a star ⭐ or cite us using:

```bibtex
@inproceedings{magis,
  author       = {Wei Tao and
                  Yucheng Zhou and
                  Yanlin Wang and
                  Wenqiang Zhang and
                  Hongyu Zhang and
                  Yu Cheng},
  title        = {{MAGIS:} LLM-Based Multi-Agent Framework for GitHub Issue Resolution},
  booktitle    = {NeurIPS},
  year         = {2024}
}
```

## 🙏 Acknowledgements

- We thank the prior work, **[SWE-bench](https://arxiv.org/abs/2310.06770)** and [their responses for my question](https://github.com/SWE-bench/SWE-bench/issues/23).

- We gratefully acknowledge [OpenMoji](https://openmoji.org/) for providing the emojis used in our demonstrations.