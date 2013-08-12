describe "Course Overview", ->

    beforeEach ->
        _.each ["/static/js/vendor/date.js", "/static/js/vendor/timepicker/jquery.timepicker.js", "/jsi18n/", "/static/js/vendor/draggabilly.pkgd.js"], (path) ->
          appendSetFixtures """
            <script type="text/javascript" src="#{path}"></script>
          """

        appendSetFixtures """
            <div class="section-published-date">
              <span class="published-status">
                <strong>Will Release:</strong> 06/12/2013 at 04:00 UTC
              </span>
              <a href="#" class="edit-button" data-date="06/12/2013" data-time="04:00" data-id="i4x://pfogg/42/chapter/d6b47f7b084f49debcaf67fe5436c8e2">Edit</a>
           </div>
        """

        appendSetFixtures """
          <div class="edit-subsection-publish-settings">
            <div class="settings">
              <h3>Section Release Date</h3>
              <div class="picker datepair">
                <div class="field field-start-date">
                  <label for="">Release Day</label>
                  <input class="start-date date" type="text" name="start_date" value="04/08/1990" placeholder="MM/DD/YYYY" class="date" size='15' autocomplete="off"/>
              </div>
              <div class="field field-start-time">
                <label for="">Release Time (<abbr title="Coordinated Universal Time">UTC</abbr>)</label>
                <input class="start-time time" type="text" name="start_time" value="12:00" placeholder="HH:MM" class="time" size='10' autocomplete="off"/>
              </div>
              <div class="description">
                <p>On the date set above, this section – <strong class="section-name"></strong> – will be released to students. Any units marked private will only be visible to admins.</p>
              </div>
            </div>
            <a href="#" class="save-button">Save</a><a href="#" class="cancel-button">Cancel</a>
          </div>
        </div>
        """

        appendSetFixtures """
          <section class="courseware-section branch" data-id="a-location-goes-here">
            <li class="branch collapsed id-holder" data-id="an-id-goes-here">
              <a href="#" class="delete-section-button"></a>
            </li>
          </section>
        """

        appendSetFixtures """
          <div class="subsection-list">
            <ol class="sortable-unit-list" data-id="parent-list-id">
              <li class="unit" data-id="first-unit-id" data-parent-id="parent-list-id"></li>
              <li class="unit" data-id="second-unit-id" data-parent-id="parent-list-id"></li>
              <li class="unit" data-id="third-unit-id" data-parent-id="parent-list-id"></li>
            </ol>
          </div>
        """#"

        spyOn(window, 'saveSetSectionScheduleDate').andCallThrough()
        # Have to do this here, as it normally gets bound in document.ready()
        $('a.save-button').click(saveSetSectionScheduleDate)
        $('a.delete-section-button').click(deleteSection)
        $(".edit-subsection-publish-settings .start-date").datepicker()

        @notificationSpy = spyOn(CMS.Views.Notification.Mini.prototype, 'show').andCallThrough()
        window.analytics = jasmine.createSpyObj('analytics', ['track'])
        window.course_location_analytics = jasmine.createSpy()
        @xhr = sinon.useFakeXMLHttpRequest()
        requests = @requests = []
        @xhr.onCreate = (req) -> requests.push(req)

        CMS.Views.Draggabilly.makeDraggable(
          '.unit',
          '.unit-drag-handle',
          'ol.sortable-unit-list',
          'li.branch, article.subsection-body'
        )

    afterEach ->
        delete window.analytics
        delete window.course_location_analytics
        @notificationSpy.reset()

    it "should save model when save is clicked", ->
        $('a.edit-button').click()
        $('a.save-button').click()
        expect(saveSetSectionScheduleDate).toHaveBeenCalled()

    it "should show a confirmation on save", ->
        $('a.edit-button').click()
        $('a.save-button').click()
        expect(@notificationSpy).toHaveBeenCalled()

    it "should delete model when delete is clicked", ->
      deleteSpy = spyOn(window, '_deleteItem').andCallThrough()
      $('a.delete-section-button').click()
      $('a.action-primary').click()
      expect(deleteSpy).toHaveBeenCalled()
      expect(@requests[0].url).toEqual('/delete_item')

    it "should not delete model when cancel is clicked", ->
      deleteSpy = spyOn(window, '_deleteItem').andCallThrough()
      $('a.delete-section-button').click()
      $('a.action-secondary').click()
      expect(@requests.length).toEqual(0)

    it "should show a confirmation on delete", ->
      $('a.delete-section-button').click()
      $('a.action-primary').click()
      expect(@notificationSpy).toHaveBeenCalled()

    it "correctly finds the drop target of a drag", ->
      $ele = $('.unit').first()
      $ele.offset(
        top: $ele.offset().top + 20, left: $ele.offset().left
      )
      destination = CMS.Views.Draggabilly.findDestination($ele)
      expect(destination.ele).toBe($ele.siblings().first())
      expect(destination.attachMethod).toBe('after')

    it "reports a null destination on a failed drag", ->
      $ele = $('.unit').first()
      $ele.offset(
        top: $ele.offset().top + 200, left: $ele.offset().left
      )
      destination = CMS.Views.Draggabilly.findDestination($ele)
      expect(destination).toEqual(
        ele: null
        attachMethod: ""
      )
