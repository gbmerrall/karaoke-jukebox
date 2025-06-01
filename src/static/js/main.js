// main.js
// Custom JavaScript for the Karaoke App
console.log("main.js loaded"); 

document.addEventListener("DOMContentLoaded", function() {
  // Add loading animation for search
  const searchForm = document.getElementById("search-form");
  if (searchForm) {
    searchForm.addEventListener("submit", function(e) {
      const resultsDiv = document.getElementById("search-results");
      if (resultsDiv) {
        resultsDiv.innerHTML = '<div class="d-flex justify-content-center my-4"><div class="loader"></div></div>';
      }
    });
  }

  // Set up direct button state changes
  document.addEventListener('click', function(e) {
    // Check if a queue button was clicked
    if (e.target && e.target.classList.contains('queue-btn') && !e.target.disabled) {
      // Change the button text and style to "Wait..."
      e.target.textContent = 'Wait...';
      e.target.classList.remove('btn-success');
      e.target.classList.add('btn-waiting');
    }
  });

  // Handle completed requests
  document.body.addEventListener('htmx:afterRequest', function(event) {
    // Get the element that triggered the request
    const elt = event.detail.elt;
    
    // Only handle queue add requests
    if (elt.tagName === 'FORM' && elt.action.includes('/queue/add')) {
      // Find the youtube ID from the form
      const inputs = elt.querySelectorAll('input');
      let youtubeId = null;
      
      for (let input of inputs) {
        if (input.name === 'youtube_id') {
          youtubeId = input.value;
          break;
        }
      }
      
      if (youtubeId) {
        // Find all buttons for this video ID and update them
        const buttons = document.querySelectorAll(`button[id="queue-btn-${youtubeId}"]`);
        buttons.forEach(function(btn) {
          btn.textContent = 'Done';
          btn.classList.remove('btn-waiting', 'btn-success');
          btn.classList.add('btn-success', 'disabled');
          btn.disabled = true;
        });
        
        console.log('Queue operation completed for:', youtubeId);
      }
    }
  });
  
  // For direct button clicks without HTMX
  document.body.addEventListener('htmx:beforeRequest', function(event) {
    // Cancel event propagation for disabled buttons
    if (event.detail.elt.disabled) {
      event.preventDefault();
    }
  });

  // SSE for real-time queue updates
  const queueContainer = document.getElementById('queue-container');
  if (queueContainer && queueContainer.querySelector('#queue-bar, .text-muted')) { // Check if queue-bar or placeholder is present
    const evtSource = new EventSource("/queue/stream");
    evtSource.onmessage = function(event) {
      // When a queue update is received, reload the queue bar by updating the container
      if (window.htmx) {
        // Ensure the container is still in the DOM before attempting to update
        if (document.getElementById('queue-container')) {
          htmx.ajax('GET', '/queue/bar', {target: '#queue-container', swap: 'innerHTML'});
        } else {
          // If container is gone, perhaps close the EventSource
          evtSource.close();
          console.log("Queue container not found, SSE stopped.");
        }
      }
    };
  }
}); 