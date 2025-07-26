document.querySelectorAll('.toggleButton').forEach(function (button) {
    var heightLimit = 168;

    var chatMessageDiv = button.closest('.chat-message');
    var userMessageDiv = chatMessageDiv.querySelector('.user-message');

    if (userMessageDiv.scrollHeight > heightLimit) {
        button.style.display = 'block';
    } else {
        button.style.display = 'none';
    }

    button.addEventListener('click', function () {
        userMessageDiv.classList.toggle('expanded');

        this.textContent = this.textContent === 'Expand' ? 'Collapse' : 'Expand';

        chatMessageDiv.scrollIntoView({ behavior: 'smooth' });
    });
});



document.querySelectorAll('.user-icon').forEach(function (userIcon) {
    userIcon.addEventListener('click', function () {
        var chatMessage = this.closest('.chat-message');

        var infoBox = chatMessage.querySelector('.info-box');

        if (infoBox.style.display === "none") {
            infoBox.style.display = "block";

            setTimeout(function () {
                infoBox.style.display = "none";
            }, 20000);
        } else {
            infoBox.style.display = "none";
        }
    });
});


hljs.highlightAll();

window.onscroll = function() {scrollFunction()};

function scrollFunction() {
  let scrollTotal = document.documentElement.scrollHeight - window.innerHeight;
  let scrollCurrent = window.scrollY;

  if (scrollCurrent > 20) {
    document.getElementById("backToTop").style.display = "block";
    document.getElementById("goToBottom").style.display = "none";
  } else if (scrollCurrent < scrollTotal - 20) {
    document.getElementById("goToBottom").style.display = "block";
    document.getElementById("backToTop").style.display = "none";
  } else {
    document.getElementById("backToTop").style.display = "none";
    document.getElementById("goToBottom").style.display = "none";
  }
}

document.getElementById("backToTop").onclick = function() {
  window.scrollTo({top: 0, behavior: 'smooth'});
};

document.getElementById("goToBottom").onclick = function() {
  window.scrollTo({top: document.body.scrollHeight, behavior: 'smooth'});
};
window.onscroll = function () { scrollFunction() };

function scrollFunction() {
    let scrollTotal = document.documentElement.scrollHeight - window.innerHeight;
    let scrollCurrent = window.scrollY;

    if (scrollCurrent > 20) {
        document.getElementById("backToTop").style.display = "block";
        document.getElementById("goToBottom").style.display = "none";
    } else if (scrollCurrent < scrollTotal - 20) {
        document.getElementById("goToBottom").style.display = "block";
        document.getElementById("backToTop").style.display = "none";
    } else {
        document.getElementById("backToTop").style.display = "none";
        document.getElementById("goToBottom").style.display = "none";
    }
}

document.getElementById("backToTop").onclick = function () {
    window.scrollTo({ top: 0, behavior: 'smooth' });
};

document.getElementById("goToBottom").onclick = function () {
    window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });
};
